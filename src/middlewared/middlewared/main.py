from .apidocs import app as apidocs_app
from .client import ejson as json
from .event import EventSource, Events
from .job import Job, JobsQueue
from .pipe import Pipes, Pipe
from .restful import RESTfulAPI
from .schema import Error as SchemaError
from .service_exception import adapt_exception, CallError, CallException, ValidationError, ValidationErrors
from .utils import osc, start_daemon_thread, sw_version, LoadPluginsMixin
from .utils.debug import get_frame_details, get_threads_stacks
from .utils.lock import SoftHardSemaphore, SoftHardSemaphoreLimit
from .utils.io_thread_pool_executor import IoThreadPoolExecutor
from .utils.profile import profile_wrap
from .utils.run_in_thread import RunInThreadMixin
from .webui_auth import WebUIAuth
from .worker import main_worker, worker_init
from aiohttp import web
from aiohttp.web_exceptions import HTTPPermanentRedirect
from aiohttp.web_middlewares import normalize_path_middleware
from aiohttp_wsgi import WSGIHandler
from collections import defaultdict

import argparse
import asyncio
import binascii
from collections import namedtuple
import concurrent.futures
import concurrent.futures.process
import concurrent.futures.thread
import errno
import fcntl
import functools
import inspect
import itertools
import multiprocessing
import os
import pickle
import re
import queue
import select
import setproctitle
import signal
import struct
import sys
import termios
import threading
import time
import traceback
import types
import urllib.parse
import uuid

from . import logger

if osc.IS_LINUX:
    from systemd.daemon import notify as systemd_notify


class Application(object):

    def __init__(self, middleware, loop, request, response):
        self.middleware = middleware
        self.loop = loop
        self.request = request
        self.response = response
        self.authenticated = False
        self.handshake = False
        self.logger = logger.Logger('application').getLogger()
        self.session_id = str(uuid.uuid4())
        self.rest = False
        self.websocket = True

        # Allow at most 10 concurrent calls and only queue up until 20
        self._softhardsemaphore = SoftHardSemaphore(10, 20)
        self._py_exceptions = False

        """
        Callback index registered by services. They are blocking.

        Currently the following events are allowed:
          on_message(app, message)
          on_close(app)
        """
        self.__callbacks = defaultdict(list)
        self.__event_sources = {}
        self.__subscribed = {}

    def register_callback(self, name, method):
        assert name in ('on_message', 'on_close')
        self.__callbacks[name].append(method)

    def _send(self, data):
        asyncio.run_coroutine_threadsafe(self.response.send_str(json.dumps(data)), loop=self.loop)

    def _tb_error(self, exc_info):
        klass, exc, trace = exc_info

        frames = []
        cur_tb = trace
        while cur_tb:
            tb_frame = cur_tb.tb_frame
            cur_tb = cur_tb.tb_next

            cur_frame = get_frame_details(tb_frame, self.logger)
            if cur_frame:
                frames.append(cur_frame)

        return {
            'class': klass.__name__,
            'frames': frames,
            'formatted': ''.join(traceback.format_exception(*exc_info)),
        }

    def send_error(self, message, errno, reason=None, exc_info=None, etype=None, extra=None):
        error_extra = {}
        if self._py_exceptions and exc_info:
            error_extra['py_exception'] = binascii.b2a_base64(pickle.dumps(exc_info[1])).decode()
        self._send({
            'msg': 'result',
            'id': message['id'],
            'error': dict({
                'error': errno,
                'type': etype,
                'reason': reason,
                'trace': self._tb_error(exc_info) if exc_info else None,
                'extra': extra,
            }, **error_extra),
        })

    async def call_method(self, message, serviceobj, methodobj):
        params = message.get('params') or []

        try:
            async with self._softhardsemaphore:
                result = await self.middleware._call(message['method'], serviceobj, methodobj, params, app=self,
                                                     io_thread=False)
            if isinstance(result, Job):
                result = result.id
            elif isinstance(result, types.GeneratorType):
                result = list(result)
            elif isinstance(result, types.AsyncGeneratorType):
                result = [i async for i in result]
            self._send({
                'id': message['id'],
                'msg': 'result',
                'result': result,
            })
        except SoftHardSemaphoreLimit as e:
            self.send_error(
                message,
                errno.ETOOMANYREFS,
                f'Maximum number of concurrent calls ({e.args[0]}) has exceeded.',
            )
        except ValidationError as e:
            self.send_error(message, e.errno, str(e), sys.exc_info(), etype='VALIDATION', extra=[
                (e.attribute, e.errmsg, e.errno),
            ])
        except ValidationErrors as e:
            self.send_error(message, errno.EAGAIN, str(e), sys.exc_info(), etype='VALIDATION', extra=list(e))
        except (CallException, SchemaError) as e:
            # CallException and subclasses are the way to gracefully
            # send errors to the client
            self.send_error(message, e.errno, str(e), sys.exc_info(), extra=e.extra)
        except Exception as e:
            adapted = adapt_exception(e)
            if adapted:
                self.send_error(message, adapted.errno, str(adapted), sys.exc_info(), extra=adapted.extra)
            else:
                self.send_error(message, errno.EINVAL, str(e), sys.exc_info())
                if not self._py_exceptions:
                    self.logger.warn('Exception while calling {}(*{})'.format(
                        message['method'],
                        self.middleware.dump_args(message.get('params', []), method_name=message['method'])
                    ), exc_info=True)
                    asyncio.ensure_future(self.__crash_reporting(sys.exc_info()))

    async def __crash_reporting(self, exc_info):
        if self.middleware.crash_reporting.is_disabled():
            self.logger.debug('[Crash Reporting] is disabled using sentinel file.')
        elif self.middleware.crash_reporting_semaphore.locked():
            self.logger.debug('[Crash Reporting] skipped due too many running instances')
        else:
            async with self.middleware.crash_reporting_semaphore:
                extra_log_files = (('/var/log/middlewared.log', 'middlewared_log'),)
                await self.middleware.run_in_thread(
                    self.middleware.crash_reporting.report,
                    exc_info,
                    extra_log_files,
                )

    async def subscribe(self, ident, name):

        if ':' in name:
            shortname, arg = name.split(':', 1)
        else:
            shortname = name
            arg = None
        event_source = self.middleware.get_event_source(shortname)
        if event_source:
            for v in self.__event_sources.values():
                # Do not allow an event source to be subscribed again
                if v['name'] == name:
                    self._send({
                        'msg': 'nosub',
                        'id': ident,
                        'error': {
                            'error': 'Already subscribed',
                        }
                    })
                    return
            es = event_source(self.middleware, self, ident, name, arg)
            self.__event_sources[ident] = {
                'event_source': es,
                'name': name,
            }
            # Start it after setting __event_sources or it can have a race condition
            start_daemon_thread(target=es.process)
        else:
            self.__subscribed[ident] = name

        self._send({
            'msg': 'ready',
            'subs': [ident],
        })

    async def unsubscribe(self, ident):
        if ident in self.__subscribed:
            self.__subscribed.pop(ident)
        elif ident in self.__event_sources:
            event_source = self.__event_sources[ident]['event_source']
            await self.middleware.run_in_thread(event_source.cancel)
            self.__event_sources.pop(ident)

    def send_event(self, name, event_type, **kwargs):
        if (
            not any(i == name or i == '*' for i in self.__subscribed.values()) and
            not any(i['name'] == name for i in self.__event_sources.values())
        ):
            return
        event = {
            'msg': event_type.lower(),
            'collection': name,
        }
        kwargs = kwargs.copy()
        if 'id' in kwargs:
            event['id'] = kwargs.pop('id')
        if event_type in ('ADDED', 'CHANGED'):
            if 'fields' in kwargs:
                event['fields'] = kwargs.pop('fields')
        if event_type == 'CHANGED':
            if 'cleared' in kwargs:
                event['cleared'] = kwargs.pop('cleared')
        if kwargs:
            event['extra'] = kwargs
        self._send(event)

    def on_open(self):
        self.middleware.register_wsclient(self)

    async def on_close(self, *args, **kwargs):
        # Run callbacks registered in plugins for on_close
        for method in self.__callbacks['on_close']:
            try:
                method(self)
            except Exception:
                self.logger.error('Failed to run on_close callback.', exc_info=True)

        for ident, val in self.__event_sources.items():
            event_source = val['event_source']
            asyncio.ensure_future(self.middleware.run_in_thread(event_source.cancel))

        self.middleware.unregister_wsclient(self)

    async def on_message(self, message):
        # Run callbacks registered in plugins for on_message
        for method in self.__callbacks['on_message']:
            try:
                method(self, message)
            except Exception:
                self.logger.error('Failed to run on_message callback.', exc_info=True)

        if message['msg'] == 'connect':
            if message.get('version') != '1':
                self._send({
                    'msg': 'failed',
                    'version': '1',
                })
            else:
                features = message.get('features') or []
                if 'PY_EXCEPTIONS' in features:
                    self._py_exceptions = True
                # aiohttp can cancel tasks if a request take too long to finish
                # It is desired to prevent that in this stage in case we are debugging
                # middlewared via gdb (which makes the program execution a lot slower)
                await asyncio.shield(self.middleware.call_hook('core.on_connect', app=self))
                self._send({
                    'msg': 'connected',
                    'session': self.session_id,
                })
                self.handshake = True
            return

        if not self.handshake:
            self._send({
                'msg': 'failed',
                'version': '1',
            })
            return

        if message['msg'] == 'method':
            try:
                serviceobj, methodobj = self.middleware._method_lookup(message['method'])
            except CallError as e:
                self.send_error(message, e.errno, str(e), sys.exc_info(), extra=e.extra)
                return

            if not self.authenticated and not hasattr(methodobj, '_no_auth_required'):
                self.send_error(message, errno.EACCES, 'Not authenticated')
                return

            asyncio.ensure_future(self.call_method(message, serviceobj, methodobj))
            return
        elif message['msg'] == 'ping':
            pong = {'msg': 'pong'}
            if 'id' in message:
                pong['id'] = message['id']
            self._send(pong)
            return

        if not self.authenticated:
            self.send_error(message, errno.EACCES, 'Not authenticated')
            return

        if message['msg'] == 'sub':
            await self.subscribe(message['id'], message['name'])
        elif message['msg'] == 'unsub':
            await self.unsubscribe(message['id'])

    def __getstate__(self):
        return {}

    def __setstate__(self, newstate):
        pass


class FileApplication(object):

    def __init__(self, middleware, loop):
        self.middleware = middleware
        self.loop = loop
        self.jobs = {}

    def register_job(self, job_id):
        self.jobs[job_id] = self.middleware.loop.call_later(
            60, lambda: asyncio.ensure_future(self._cleanup_job(job_id)))

    async def _cleanup_cancel(self, job_id):
        job_cleanup = self.jobs.pop(job_id, None)
        if job_cleanup:
            job_cleanup.cancel()

    async def _cleanup_job(self, job_id):
        if job_id not in self.jobs:
            return
        self.jobs[job_id].cancel()
        del self.jobs[job_id]

        job = self.middleware.jobs[job_id]
        await job.pipes.close()

    async def download(self, request):
        path = request.path.split('/')
        if not request.path[-1].isdigit():
            resp = web.Response()
            resp.set_status(404)
            return resp

        job_id = int(path[-1])

        qs = urllib.parse.parse_qs(request.query_string)
        denied = False
        filename = None
        if 'auth_token' not in qs:
            denied = True
        else:
            auth_token = qs.get('auth_token')[0]
            token = await self.middleware.call('auth.get_token', auth_token)
            if not token:
                denied = True
            else:
                if token['attributes'].get('job') != job_id:
                    denied = True
                else:
                    filename = token['attributes'].get('filename')
        if denied:
            resp = web.Response()
            resp.set_status(401)
            return resp

        job = self.middleware.jobs.get(job_id)
        if not job:
            resp = web.Response()
            resp.set_status(404)
            return resp

        if job_id not in self.jobs:
            resp = web.Response()
            resp.set_status(410)
            return resp

        resp = web.StreamResponse(status=200, reason='OK', headers={
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Transfer-Encoding': 'chunked',
        })
        await resp.prepare(request)

        def do_copy():
            while True:
                read = job.pipes.output.r.read(1048576)
                if read == b'':
                    break
                asyncio.run_coroutine_threadsafe(resp.write(read), loop=self.loop).result()

        try:
            await self._cleanup_cancel(job_id)
            await self.middleware.run_in_thread(do_copy)
        finally:
            await job.pipes.close()

        await resp.drain()
        return resp

    async def upload(self, request):
        denied = True
        auth = request.headers.get('Authorization')
        if auth:
            if auth.startswith('Basic '):
                try:
                    auth = binascii.a2b_base64(auth[6:]).decode()
                    if ':' in auth:
                        user, password = auth.split(':', 1)
                        if await self.middleware.call('auth.check_user', user, password):
                            denied = False
                except binascii.Error:
                    pass
            elif auth.startswith('Token '):
                auth_token = auth.split(" ", 1)[1]
                token = await self.middleware.call('auth.get_token', auth_token)

                if token:
                    denied = False
        else:
            qs = urllib.parse.parse_qs(request.query_string)
            if 'auth_token' in qs:
                auth_token = qs.get('auth_token')[0]
                token = await self.middleware.call('auth.get_token', auth_token)
                if token:
                    denied = False

        if denied:
            resp = web.Response()
            resp.set_status(401)
            return resp

        reader = await request.multipart()

        part = await reader.next()
        if not part:
            resp = web.Response(status=405, body='No part found on payload')
            resp.set_status(405)
            return resp

        if part.name != 'data':
            resp = web.Response(status=405, body='"data" part must be the first on payload')
            resp.set_status(405)
            return resp

        try:
            data = json.loads(await part.read())
        except Exception as e:
            return web.Response(status=400, body=str(e))

        if 'method' not in data:
            return web.Response(status=422)

        filepart = await reader.next()

        if not filepart or filepart.name != 'file':
            resp = web.Response(status=405, body='"file" not found as second part on payload')
            resp.set_status(405)
            return resp

        def copy():
            try:
                try:
                    while True:
                        read = asyncio.run_coroutine_threadsafe(
                            filepart.read_chunk(filepart.chunk_size),
                            loop=self.loop,
                        ).result()
                        if read == b'':
                            break
                        job.pipes.input.w.write(read)
                finally:
                    job.pipes.input.w.close()
            except BrokenPipeError:
                pass

        try:
            job = await self.middleware.call(data['method'], *(data.get('params') or []),
                                             pipes=Pipes(input=self.middleware.pipe()))
            await self.middleware.run_in_thread(copy)
        except CallError as e:
            if e.errno == CallError.ENOMETHOD:
                status_code = 422
            else:
                status_code = 412
            return web.Response(status=status_code, body=str(e))
        except Exception as e:
            return web.Response(status=500, body=str(e))

        resp = web.Response(
            status=200,
            headers={
                'Content-Type': 'application/json',
            },
            body=json.dumps({'job_id': job.id}).encode(),
        )
        return resp


ShellResize = namedtuple("ShellResize", ["cols", "rows"])


class ShellWorkerThread(threading.Thread):
    """
    Worker thread responsible for forking and running the shell
    and spawning the reader and writer threads.
    """

    def __init__(self, ws, input_queue, loop, jail=None):
        self.ws = ws
        self.input_queue = input_queue
        self.loop = loop
        self.shell_pid = None
        self.jail = jail
        self._die = False
        super(ShellWorkerThread, self).__init__(daemon=True)

    def resize(self, cols, rows):
        self.input_queue.put(ShellResize(cols, rows))

    def run(self):

        self.shell_pid, master_fd = os.forkpty()
        if self.shell_pid == 0:
            osc.close_fds(3)

            os.chdir('/root')
            cmd = [
                '/usr/bin/login', '-p', '-f', 'root',
            ]

            if self.jail is not None:
                cmd = [
                    '/usr/local/bin/iocage',
                    'console',
                    '-f',
                    self.jail
                ]
            os.execve(cmd[0], cmd, {
                'TERM': 'xterm',
                'HOME': '/root',
                'LANG': 'en_US.UTF-8',
                'PATH': '/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin:/root/bin',
            })

        # Terminal baudrate affects input queue size
        attr = termios.tcgetattr(master_fd)
        attr[4] = attr[5] = termios.B921600
        termios.tcsetattr(master_fd, termios.TCSANOW, attr)

        def reader():
            """
            Reader thread for reading from pty file descriptor
            and forwarding it to the websocket.
            """
            while True:
                read = os.read(master_fd, 1024)
                if read == b'':
                    break
                asyncio.run_coroutine_threadsafe(
                    self.ws.send_str(read.decode('utf8')), loop=self.loop
                ).result()

        def writer():
            """
            Writer thread for reading from input_queue and write to
            the shell pty file descriptor.
            """
            while True:
                try:
                    get = self.input_queue.get(timeout=1)
                    if isinstance(get, ShellResize):
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", get.rows, get.cols, 0, 0))
                    else:
                        os.write(master_fd, get)
                except queue.Empty:
                    # If we timeout waiting in input query lets make sure
                    # the shell process is still alive
                    try:
                        os.kill(self.shell_pid, 0)
                    except ProcessLookupError:
                        break

        t_reader = threading.Thread(target=reader, daemon=True)
        t_reader.start()

        t_writer = threading.Thread(target=writer, daemon=True)
        t_writer.start()

        # Wait for shell to exit
        while True:
            try:
                pid, rv = os.waitpid(self.shell_pid, os.WNOHANG)
            except ChildProcessError:
                break
            if self._die:
                return
            if pid <= 0:
                time.sleep(1)

        t_reader.join()
        t_writer.join()
        asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)

    def die(self):
        self._die = True


class ShellConnectionData(object):
    id = None
    t_worker = None


class ShellApplication(object):
    shells = {}

    def __init__(self, middleware):
        self.middleware = middleware

    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        conndata = ShellConnectionData()
        conndata.id = str(uuid.uuid4())

        try:
            await self.run(ws, request, conndata)
        except Exception:
            if conndata.t_worker:
                await self.worker_kill(conndata.t_worker)
        finally:
            self.shells.pop(conndata.id, None)
            return ws

    async def run(self, ws, request, conndata):

        # Each connection will have its own input queue
        input_queue = queue.Queue()
        authenticated = False

        async for msg in ws:
            if authenticated:
                # Add content of every message received in input queue
                try:
                    input_queue.put(msg.data.encode())
                except UnicodeEncodeError:
                    # Should we handle Encode error?
                    # xterm.js seems to operate with the websocket in text mode,
                    pass
            else:
                try:
                    data = json.loads(msg.data)
                except json.decoder.JSONDecodeError:
                    continue

                token = data.get('token')
                if not token:
                    continue

                token = await self.middleware.call('auth.get_token', token)
                if not token:
                    await ws.send_json({
                        'msg': 'failed',
                        'error': {
                            'error': errno.EACCES,
                            'reason': 'Invalid token',
                        }
                    })
                    continue

                authenticated = True
                await ws.send_json({
                    'msg': 'connected',
                    'id': conndata.id,
                })

                jail = data.get('jail')
                conndata.t_worker = ShellWorkerThread(ws=ws, input_queue=input_queue, loop=asyncio.get_event_loop(), jail=jail)
                conndata.t_worker.start()

                self.shells[conndata.id] = conndata.t_worker

        # If connection was not authenticated, return earlier
        if not authenticated:
            return ws

        if conndata.t_worker:
            asyncio.ensure_future(self.worker_kill(conndata.t_worker))

        return ws

    async def worker_kill(self, t_worker):
        # If connection has been closed lets make sure shell is killed
        if t_worker.shell_pid:

            try:
                kqueue = select.kqueue()
                kevent = select.kevent(t_worker.shell_pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ENABLE, select.KQ_NOTE_EXIT)
                kqueue.control([kevent], 0)

                os.kill(t_worker.shell_pid, signal.SIGTERM)

                # If process has not died in 2 seconds, try the big gun
                events = await self.middleware.run_in_thread(kqueue.control, None, 1, 2)
                if not events:
                    os.kill(t_worker.shell_pid, signal.SIGKILL)

                    # If process has not died even with the big gun
                    # There is nothing else we can do, leave it be and
                    # release the worker thread
                    events = await self.middleware.run_in_thread(kqueue.control, None, 1, 2)
                    if not events:
                        t_worker.die()
            except ProcessLookupError:
                pass

        # Wait thread join in yet another thread to avoid event loop blockage
        # There may be a simpler/better way to do this?
        await self.middleware.run_in_thread(t_worker.join)


class Middleware(LoadPluginsMixin, RunInThreadMixin):

    CONSOLE_ONCE_PATH = '/tmp/.middlewared-console-once'

    def __init__(
        self, loop_debug=False, loop_monitor=True, overlay_dirs=None, debug_level=None,
        log_handler=None, startup_seq_path=None,
        log_format='[%(asctime)s] (%(levelname)s) %(name)s.%(funcName)s():%(lineno)d - %(message)s'
    ):
        super().__init__(overlay_dirs)
        self.logger = logger.Logger(
            'middlewared', debug_level, log_format
        ).getLogger()
        self.logger.info('Starting %s middleware', sw_version())
        self.crash_reporting = logger.CrashReporting()
        self.crash_reporting_semaphore = asyncio.Semaphore(value=2)
        self.loop_debug = loop_debug
        self.loop_monitor = loop_monitor
        self.debug_level = debug_level
        self.log_handler = log_handler
        self.log_format = log_format
        self.startup_seq = 0
        self.startup_seq_path = startup_seq_path
        self.app = None
        self.loop = None
        self.run_in_thread_executor = IoThreadPoolExecutor('IoThread', 20)
        self.__thread_id = threading.get_ident()
        # Spawn new processes for ProcessPool instead of forking
        multiprocessing.set_start_method('spawn')
        self.__ws_threadpool = concurrent.futures.ThreadPoolExecutor(
            initializer=lambda: osc.set_thread_name('threadpool_ws'),
            max_workers=10,
        )
        self.__init_procpool()
        self.__wsclients = {}
        self.__events = Events()
        self.__event_sources = {}
        self.__event_subs = defaultdict(list)
        self.__hooks = defaultdict(list)
        self.__server_threads = []
        self.__init_services()
        self.__console_io = False if os.path.exists(self.CONSOLE_ONCE_PATH) else None
        self.__terminate_task = None
        self.jobs = JobsQueue(self)

    def __init_services(self):
        from middlewared.service import CoreService
        self.add_service(CoreService(self))
        self.event_register('core.environ', 'Send on middleware process environment changes.', private=True)

    async def __plugins_load(self):

        setup_funcs = []

        def on_module_begin(mod):
            self._console_write(f'loaded plugin {mod.__name__}')
            self.__notify_startup_progress()

        def on_module_end(mod):
            if not hasattr(mod, 'setup'):
                return

            mod_name = mod.__name__.split('.')
            setup_plugin = mod_name[mod_name.index('plugins') + 1]

            setup_funcs.append((setup_plugin, mod.setup))

        def on_modules_loaded():
            self._console_write(f'resolving plugins schemas')

        self._load_plugins(
            on_module_begin=on_module_begin,
            on_module_end=on_module_end,
            on_modules_loaded=on_modules_loaded,
        )

        return setup_funcs

    async def __plugins_setup(self, setup_funcs):

        # TODO: Rework it when we have order defined for setup functions
        def sort_key(plugin__function):
            plugin, function = plugin__function

            beginning = [
                'datastore',
                # Allow internal UNIX socket authentication for plugins that run in separate pools
                'auth',
                # We need to register all services because pseudo-services can still be used by plugins setup functions
                'service',
                # We run boot plugin first to ensure we are able to retrieve
                # BOOT POOL during system plugin initialization
                'boot',
                # We need to run system plugin setup's function first because when system boots, the right
                # timezone is not configured. See #72131
                'system',
                # We also need to load alerts first because other plugins can issue one-shot alerts during their
                # initialization
                'alert',
                # Migrate users and groups ASAP
                'account',
            ]
            try:
                return beginning.index(plugin)
            except ValueError:
                return len(beginning)
        setup_funcs = sorted(setup_funcs, key=sort_key)

        # Only call setup after all schemas have been resolved because
        # they can call methods with schemas defined.
        setup_total = len(setup_funcs)
        for i, setup_func in enumerate(setup_funcs):
            name, f = setup_func
            self._console_write(f'setting up plugins ({name}) [{i + 1}/{setup_total}]')
            self.__notify_startup_progress()
            call = f(self)
            # Allow setup to be a coroutine
            if asyncio.iscoroutinefunction(f):
                await call

        self.logger.debug('All plugins loaded')

    def _setup_periodic_tasks(self):
        for service_name, service_obj in self.get_services().items():
            for task_name in dir(service_obj):
                method = getattr(service_obj, task_name)
                if callable(method) and hasattr(method, "_periodic"):
                    if method._periodic.run_on_start:
                        delay = 0
                    else:
                        delay = method._periodic.interval

                    method_name = f'{service_name}.{task_name}'
                    self.logger.debug(f"Setting up periodic task {method_name} to run every {method._periodic.interval} seconds")

                    self.loop.call_later(
                        delay,
                        functools.partial(
                            self.__call_periodic_task,
                            method, service_name, service_obj, method_name, method._periodic.interval
                        )
                    )

    def __call_periodic_task(self, method, service_name, service_obj, method_name, interval):
        self.loop.create_task(self.__periodic_task_wrapper(method, service_name, service_obj, method_name, interval))

    async def __periodic_task_wrapper(self, method, service_name, service_obj, method_name, interval):
        self.logger.trace("Calling periodic task %s", method_name)
        try:
            await self._call(method_name, service_obj, method)
        except Exception:
            self.logger.warning("Exception while calling periodic task", exc_info=True)

        self.loop.call_later(
            interval,
            functools.partial(
                self.__call_periodic_task,
                method, service_name, service_obj, method_name, interval
            )
        )

    def _console_write(self, text, fill_blank=True, append=False):
        """
        Helper method to write the progress of middlewared loading to the
        system console.

        There are some cases where loading will take a considerable amount of time,
        giving user at least some basic feedback is fundamental.
        """
        # False means we are running in a terminal, no console needed
        self.logger.trace('_console_write %r', text)
        if self.__console_io is False:
            return
        elif self.__console_io is None:
            if sys.stdin and sys.stdin.isatty():
                self.__console_io = False
                return
            try:
                self.__console_io = open('/dev/console', 'w')
            except Exception:
                return
            try:
                # We need to make sure we only try to write to console one time
                # in case middlewared crashes and keep writing to console in a loop.
                with open(self.CONSOLE_ONCE_PATH, 'w'):
                    pass
            except Exception:
                pass
        try:
            if append:
                self.__console_io.write(text)
            else:
                prefix = 'middlewared: '
                maxlen = 60
                text = text[:maxlen - len(prefix)]
                # new line needs to go after all the blanks
                if text.endswith('\n'):
                    newline = '\n'
                    text = text[:-1]
                else:
                    newline = ''
                if fill_blank:
                    blank = ' ' * (maxlen - (len(prefix) + len(text)))
                else:
                    blank = ''
                writes = self.__console_io.write(
                    f'\r{prefix}{text}{blank}{newline}'
                )
            self.__console_io.flush()
            return writes
        except OSError:
            self.logger.debug('Failed to write to console', exc_info=True)
        except Exception:
            pass

    def __notify_startup_progress(self):
        if osc.IS_FREEBSD:
            if self.startup_seq_path is None:
                return

            with open(self.startup_seq_path + ".tmp", "w") as f:
                f.write(f"{self.startup_seq}")

            os.rename(self.startup_seq_path + ".tmp", self.startup_seq_path)

            self.startup_seq += 1

        if osc.IS_LINUX:
            systemd_notify(f'EXTEND_TIMEOUT_USEC={int(240 * 1e6)}')

    def __notify_startup_complete(self):
        if osc.IS_LINUX:
            systemd_notify('READY=1')

    def plugin_route_add(self, plugin_name, route, method):
        self.app.router.add_route('*', f'/_plugins/{plugin_name}/{route}', method)

    def get_wsclients(self):
        return self.__wsclients

    def register_wsclient(self, client):
        self.__wsclients[client.session_id] = client

    def unregister_wsclient(self, client):
        self.__wsclients.pop(client.session_id)

    def register_hook(self, name, method, sync=True, inline=False):
        """
        Register a hook under `name`.

        The given `method` will be called whenever using call_hook.
        Args:
            name(str): name of the hook, e.g. service.hook_name
            method(callable): method to be called
            sync(bool): whether the method should be called in a sync way
            inline(bool): whether the method should be called in executor's context synchronously
        """

        if inline:
            if asyncio.iscoroutinefunction(method):
                raise RuntimeError('You can\'t register coroutine function as inline hook')

            if not sync:
                raise RuntimeError('Inline hooks are always called in a sync way')

        self.__hooks[name].append({
            'method': method,
            'inline': inline,
            'sync': sync,
        })

    def _call_hook_base(self, name, *args, **kwargs):
        for hook in self.__hooks[name]:
            try:
                yield hook, hook['method'](self, *args, **kwargs)
            except Exception:
                self.logger.error(
                    'Failed to run hook {}:{}(*{}, **{})'.format(name, hook['method'], args, kwargs), exc_info=True
                )

    async def call_hook(self, name, *args, **kwargs):
        """
        Call all hooks registered under `name` passing *args and **kwargs.
        Args:
            name(str): name of the hook, e.g. service.hook_name
        """
        for hook, fut in self._call_hook_base(name, *args, **kwargs):
            try:
                if hook['inline']:
                    raise RuntimeError('Inline hooks should be called with call_hook_inline')
                elif hook['sync']:
                    await fut
                else:
                    asyncio.ensure_future(fut)
            except Exception:
                self.logger.error(
                    'Failed to run hook {}:{}(*{}, **{})'.format(name, hook['method'], args, kwargs), exc_info=True
                )

    def call_hook_sync(self, name, *args, **kwargs):
        return self.run_coroutine(self.call_hook(name, *args, **kwargs))

    def call_hook_inline(self, name, *args, **kwargs):
        for hook, fut in self._call_hook_base(name, *args, **kwargs):
            if not hook['inline']:
                raise RuntimeError('Only inline hooks can be called with call_hook_inline')

    def register_event_source(self, name, event_source):
        if not issubclass(event_source, EventSource):
            raise RuntimeError(f'{event_source} is not EventSource subclass')
        self.__event_sources[name] = event_source

    def get_event_source(self, name):
        return self.__event_sources.get(name)

    async def run_in_executor(self, pool, method, *args, **kwargs):
        """
        Runs method in a native thread using concurrent.futures.Pool.
        This prevents a CPU intensive or non-asyncio friendly method
        to block the event loop indefinitely.
        Also used to run non thread safe libraries (using a ProcessPool)
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(pool, functools.partial(method, *args, **kwargs))

    async def _run_in_conn_threadpool(self, method, *args, **kwargs):
        """
        Threads to handle websocket connection are gated on `__ws_threadpool`.
        Any other calls should use `run_in_thread` as that launches its own thread
        and does not cause deadlock waiting another thread to finish in the pool
        (which could happen on the stack call, e.g.
           service.foo calls something in using the thread pool and something also
           uses the thread pool. If service.foo is called many times before each thread
           finishes we will have a deadlock)
        """
        return await self.run_in_executor(self.__ws_threadpool, method, *args, **kwargs)

    def __init_procpool(self):
        self.__procpool = concurrent.futures.ProcessPoolExecutor(
            max_workers=5,
            initializer=functools.partial(
                worker_init, self.overlay_dirs, self.debug_level, self.log_handler
            ),
        )

    async def run_in_proc(self, method, *args, **kwargs):
        retries = 2
        for i in range(retries):
            try:
                return await self.run_in_executor(self.__procpool, method, *args, **kwargs)
            except concurrent.futures.process.BrokenProcessPool:
                if i == retries - 1:
                    raise
                self.__init_procpool()

    def pipe(self):
        return Pipe(self)

    async def _call(
        self, name, serviceobj, methodobj, params=None, app=None, pipes=None,
        job_on_progress_cb=None, io_thread=True,
    ):
        args = []
        if hasattr(methodobj, '_pass_app'):
            args.append(app)

        if params:
            args.extend(params)

        # If the method is marked as a @job we need to create a new
        # entry to keep track of its state.
        job_options = getattr(methodobj, '_job', None)
        if job_options:
            # Currently its only a boolean
            if serviceobj._config.process_pool is True:
                job_options['process'] = True
            # Create a job instance with required args
            job = Job(
                self, name, serviceobj, methodobj, args, job_options, pipes,
                on_progress_cb=job_on_progress_cb,
            )
            # Add the job to the queue.
            # At this point an `id` is assinged to the job.
            job = self.jobs.add(job)
        else:
            job = None

        if job:
            return job
        else:

            # Currently its only a boolean
            if serviceobj._config.process_pool is True:
                return await self._call_worker(name, *args)

            if asyncio.iscoroutinefunction(methodobj):
                return await methodobj(*args)

            tpool = None
            if serviceobj._config.thread_pool:
                tpool = serviceobj._config.thread_pool
            if hasattr(methodobj, '_thread_pool'):
                tpool = methodobj._thread_pool
            if tpool:
                return await self.run_in_executor(tpool, methodobj, *args)

            if io_thread:
                run_method = self.run_in_thread
            else:
                run_method = self._run_in_conn_threadpool
            return await run_method(methodobj, *args)

    async def _call_worker(self, name, *args, job=None):
        return await self.run_in_proc(main_worker, name, args, job)

    def _method_lookup(self, name):
        if '.' not in name:
            raise CallError('Invalid method name', errno.EBADMSG)
        try:
            service, method_name = name.rsplit('.', 1)
            serviceobj = self.get_service(service)
            methodobj = getattr(serviceobj, method_name)
        except (AttributeError, KeyError):
            raise CallError(f'Method "{method_name}" not found in "{service}"', CallError.ENOMETHOD)
        return serviceobj, methodobj

    def dump_args(self, args, method=None, method_name=None):
        if method is None:
            if method_name is not None:
                try:
                    method = self._method_lookup(method_name)[1]
                except Exception:
                    return args

        if not hasattr(method, 'accepts'):
            return args

        return [method.accepts[i].dump(arg) for i, arg in enumerate(args) if i < len(method.accepts)]

    async def call(self, name, *params, pipes=None, job_on_progress_cb=None, app=None, profile=False):
        serviceobj, methodobj = self._method_lookup(name)

        if profile:
            methodobj = profile_wrap(methodobj)
        return await self._call(
            name, serviceobj, methodobj, params,
            app=app, pipes=pipes, job_on_progress_cb=job_on_progress_cb, io_thread=True,
        )

    def call_sync(self, name, *params, job_on_progress_cb=None, wait=True):
        """
        Synchronous method call to be used from another thread.
        """

        serviceobj, methodobj = self._method_lookup(name)
        # This method is already being called from a thread so we cant use the same
        # thread pool or we may get in a deadlock situation if all threads in the default
        # pool are waiting.
        # Instead we launch a new thread just for that call (io_thread).
        return self.run_coroutine(
            self._call(
                name, serviceobj, methodobj, params,
                io_thread=True, job_on_progress_cb=job_on_progress_cb,
            ),
            wait=wait,
        )

    def run_coroutine(self, coro, wait=True):
        if threading.get_ident() == self.__thread_id:
            raise RuntimeError('You cannot call_sync or run_coroutine from main thread')

        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if not wait:
            return fut

        event = threading.Event()

        def done(_):
            event.set()

        fut.add_done_callback(done)

        # In case middleware dies while we are waiting for a `call_sync` result
        while not event.wait(1):
            if not self.loop.is_running():
                raise RuntimeError('Middleware is terminating')
        return fut.result()

    def get_events(self):
        return itertools.chain(
            self.__events, map(
                lambda n: (
                    n[0],
                    {
                        'description': inspect.getdoc(n[1]),
                        'private': False,
                        'wildcard_subscription': False,
                    }
                ),
                self.__event_sources.items()
            )
        )

    def event_subscribe(self, name, handler):
        """
        Internal way for middleware/plugins to subscribe to events.
        """
        self.__event_subs[name].append(handler)

    def event_register(self, name, description, private=False):
        """
        All events middleware can send should be registered so they are properly documented
        and can be browsed in documentation page without source code inspection.
        """
        self.__events.register(name, description, private=private)

    def send_event(self, name, event_type, **kwargs):

        if name not in self.__events:
            # We should eventually deny events that are not registered to ensure every event is
            # documented but for backward-compability and safety just log it for now.
            self.logger.warning(f'Event {name!r} not registered.')

        assert event_type in ('ADDED', 'CHANGED', 'REMOVED')

        self.logger.trace(f'Sending event {name!r}:{event_type!r}:{kwargs!r}')

        for session_id, wsclient in list(self.__wsclients.items()):
            try:
                wsclient.send_event(name, event_type, **kwargs)
            except Exception:
                self.logger.warn('Failed to send event {} to {}'.format(name, session_id), exc_info=True)

        # Send event also for internally subscribed plugins
        for handler in self.__event_subs.get(name, []):
            asyncio.run_coroutine_threadsafe(handler(self, event_type, kwargs), loop=self.loop)

    def pdb(self):
        import pdb
        pdb.set_trace()

    def log_threads_stacks(self):
        for thread_id, stack in get_threads_stacks().items():
            self.logger.debug('Thread %d stack:\n%s', thread_id, ''.join(stack))

    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        connection = Application(self, self.loop, request, ws)
        connection.on_open()

        try:
            async for msg in ws:
                if not connection.authenticated and len(msg.data) > 8192:
                    await ws.close(message='Anonymous connection max message length is 8 kB'.encode('utf-8'))
                    break

                x = json.loads(msg.data)
                try:
                    await connection.on_message(x)
                except Exception as e:
                    self.logger.error('Connection closed unexpectedly', exc_info=True)
                    await ws.close(message=str(e).encode('utf-8'))
                    break
        finally:
            await connection.on_close()
        return ws

    _loop_monitor_ignore_frames = (
        (
            re.compile(r'\s+File ".+/middlewared/main\.py", line [0-9]+, in run_in_thread\s+'
                       'return await self.loop.run_in_executor'),
            'run_in_thread'
        ),
    )

    def _loop_monitor_thread(self):
        """
        Thread responsible for checking current tasks that are taking too long
        to finish and printing the stack.

        DISCLAIMER/TODO: This is not free of race condition so it may show
        false positives.
        """
        osc.set_thread_name('loop_monitor')
        last = None
        while True:
            time.sleep(2)
            current = asyncio.current_task(loop=self.loop)
            if current is None:
                last = None
                continue
            if last == current:
                frame = sys._current_frames()[self.__thread_id]
                stack = traceback.format_stack(frame, limit=10)
                for regex, name in self._loop_monitor_ignore_frames:
                    if any(regex.match(s) for s in stack):
                        self.logger.warn('%s seems to be blocking event loop', name)
                        break
                else:
                    self.logger.warn(''.join(['Task seems blocked:\n'] + stack))
            last = current

    def run(self):

        self._console_write('starting')

        osc.set_thread_name('asyncio_loop')
        self.loop = asyncio.get_event_loop()

        if self.loop_debug:
            self.loop.set_debug(True)
            self.loop.slow_callback_duration = 0.2

        self.loop.create_task(self.__initialize())

        try:
            self.loop.run_forever()
        except RuntimeError as e:
            if e.args[0] != "Event loop is closed":
                raise

        # We use "_exit" specifically as otherwise process pool executor won't let middlewared process die because
        # it is still active. We don't initiate a shutdown for it because it may hang forever for any reason
        os._exit(0)

    async def __initialize(self):
        self.app = app = web.Application(middlewares=[
            normalize_path_middleware(redirect_class=HTTPPermanentRedirect)
        ], loop=self.loop)

        # Needs to happen after setting debug or may cause race condition
        # http://bugs.python.org/issue30805
        setup_funcs = await self.__plugins_load()

        self._console_write('registering services')

        if self.loop_monitor:
            # Start monitor thread after plugins have been loaded
            # because of the time spent doing I/O
            t = threading.Thread(target=self._loop_monitor_thread)
            t.setDaemon(True)
            t.start()

        self.loop.add_signal_handler(signal.SIGINT, self.terminate)
        self.loop.add_signal_handler(signal.SIGTERM, self.terminate)
        self.loop.add_signal_handler(signal.SIGUSR1, self.pdb)
        self.loop.add_signal_handler(signal.SIGUSR2, self.log_threads_stacks)

        app.router.add_route('GET', '/websocket', self.ws_handler)

        app.router.add_route('*', '/api/docs{path_info:.*}', WSGIHandler(apidocs_app))
        app.router.add_route('*', '/ui{path_info:.*}', WebUIAuth(self))

        self.fileapp = FileApplication(self, self.loop)
        app.router.add_route('*', '/_download{path_info:.*}', self.fileapp.download)
        app.router.add_route('*', '/_upload{path_info:.*}', self.fileapp.upload)

        shellapp = ShellApplication(self)
        app.router.add_route('*', '/_shell{path_info:.*}', shellapp.ws_handler)

        restful_api = RESTfulAPI(self, app)
        await restful_api.register_resources()
        asyncio.ensure_future(self.jobs.run())

        # Start up middleware worker process pool
        self.__procpool._start_queue_management_thread()

        runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await runner.setup()
        await web.UnixSite(runner, '/var/run/middlewared-internal.sock').start()

        await self.__plugins_setup(setup_funcs)

        if await self.call('system.state') == 'READY':
            self._setup_periodic_tasks()

        await web.TCPSite(runner, '0.0.0.0', 6000, reuse_address=True, reuse_port=True).start()
        await web.UnixSite(runner, '/var/run/middlewared.sock').start()

        self.logger.debug('Accepting connections')
        self._console_write('loading completed\n')

        self.__notify_startup_complete()

    def terminate(self):
        self.logger.info('Terminating')
        self.__terminate_task = self.loop.create_task(self.__terminate())

    async def __terminate(self):
        for service_name, service in self.get_services().items():
            # We're using this instead of having no-op `terminate`
            # in base class to reduce number of awaits
            if hasattr(service, "terminate"):
                self.logger.trace("Terminating %r", service)
                timeout = None
                if hasattr(service, 'terminate_timeout'):
                    try:
                        timeout = await asyncio.wait_for(self.call(f'{service_name}.terminate_timeout'), 5)
                    except Exception:
                        self.logger.error(
                            'Failed to retrieve terminate timeout value for %s', service_name, exc_info=True
                        )

                # This is to ensure if some service returns 0 as a timeout value meaning it is probably not being
                # used, we still give it the standard default 10 seconds timeout to ensure a clean exit
                timeout = timeout or 10
                try:
                    await asyncio.wait_for(service.terminate(), timeout)
                except Exception:
                    self.logger.error('Failed to terminate %s', service_name, exc_info=True)

        for task in asyncio.all_tasks(loop=self.loop):
            if task != self.__terminate_task:
                self.logger.trace("Canceling %r", task)
                task.cancel()

        self.loop.stop()


def main():
    # Workaround for development
    modpath = os.path.realpath(os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        '..',
    ))
    if modpath not in sys.path:
        sys.path.insert(0, modpath)

    parser = argparse.ArgumentParser()
    parser.add_argument('restart', nargs='?')
    parser.add_argument('--pidfile', '-P', action='store_true')
    parser.add_argument('--disable-loop-monitor', '-L', action='store_true')
    parser.add_argument('--loop-debug', action='store_true')
    parser.add_argument('--overlay-dirs', '-o', action='append')
    parser.add_argument('--debug-level', choices=[
        'TRACE',
        'DEBUG',
        'INFO',
        'WARN',
        'ERROR',
    ], default='DEBUG')
    parser.add_argument('--log-handler', choices=[
        'console',
        'file',
    ], default='console')
    args = parser.parse_args()

    pidpath = '/var/run/middlewared.pid'
    startup_seq_path = '/tmp/middlewared_startup.seq'

    if args.restart:
        if os.path.exists(pidpath):
            with open(pidpath, 'r') as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 15)
            except ProcessLookupError as e:
                if e.errno != errno.ESRCH:
                    raise

    logger.setup_logging('middleware', args.debug_level, args.log_handler)

    setproctitle.setproctitle('middlewared')

    if args.pidfile:
        with open(pidpath, "w") as _pidfile:
            _pidfile.write(f"{str(os.getpid())}\n")

    Middleware(
        loop_debug=args.loop_debug,
        loop_monitor=not args.disable_loop_monitor,
        overlay_dirs=args.overlay_dirs,
        debug_level=args.debug_level,
        log_handler=args.log_handler,
        startup_seq_path=startup_seq_path,
    ).run()


if __name__ == '__main__':
    main()
