from middlewared.schema import Bool, Dict, Int, List, Ref, Str, accepts
from middlewared.service import CallError, ConfigService, ValidationErrors, job, periodic, private
import middlewared.sqlalchemy as sa
from middlewared.validators import Email

from datetime import datetime, timedelta
from email.header import Header
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from lockfile import LockFile, LockTimeout
from mako.lookup import TemplateLookup

import base64
import errno
import html
import json
import os
import pickle
import smtplib
import socket
import syslog
import time

from requests_oauthlib import OAuth2Session


class QueueItem(object):

    def __init__(self, message):
        self.attempts = 0
        self.message = message


class MailQueue(object):

    QUEUE_FILE = '/tmp/mail.queue'
    MAX_ATTEMPTS = 3

    def __init__(self):
        self.queue = None

    def append(self, message):
        self.queue.append(QueueItem(message))

    @classmethod
    def is_empty(cls):
        if not os.path.exists(cls.QUEUE_FILE):
            return True
        try:
            return os.stat(cls.QUEUE_FILE).st_size == 0
        except OSError:
            return True

    def _get_queue(self):
        try:
            with open(self.QUEUE_FILE, 'rb') as f:
                self.queue = pickle.loads(f.read())
        except (pickle.PickleError, EOFError):
            self.queue = []

    def __enter__(self):
        self._lock = LockFile(self.QUEUE_FILE)
        while not self._lock.i_am_locking():
            try:
                self._lock.acquire(timeout=330)
            except LockTimeout:
                self._lock.break_lock()

        if not os.path.exists(self.QUEUE_FILE):
            open(self.QUEUE_FILE, 'a').close()

        self._get_queue()
        return self

    def __exit__(self, typ, value, traceback):

        with open(self.QUEUE_FILE, 'wb+') as f:
            if self.queue:
                f.write(pickle.dumps(self.queue))

        self._lock.release()
        if typ is not None:
            raise


class MailModel(sa.Model):
    __tablename__ = 'system_email'

    id = sa.Column(sa.Integer(), primary_key=True)
    em_fromemail = sa.Column(sa.String(120), default='')
    em_outgoingserver = sa.Column(sa.String(120))
    em_port = sa.Column(sa.Integer(), default=25)
    em_security = sa.Column(sa.String(120), default="plain")
    em_smtp = sa.Column(sa.Boolean())
    em_user = sa.Column(sa.String(120), nullable=True)
    em_pass = sa.Column(sa.String(120), nullable=True)
    em_fromname = sa.Column(sa.String(120), default='')
    em_oauth = sa.Column(sa.JSON(type=dict, encrypted=True), nullable=True)


class MailService(ConfigService):

    oauth_access_token = None
    oauth_access_token_expires_at = None

    class Config:
        datastore = 'system.email'
        datastore_prefix = 'em_'
        datastore_extend = 'mail.mail_extend'

    @private
    async def mail_extend(self, cfg):
        if cfg['security']:
            cfg['security'] = cfg['security'].upper()
        return cfg

    @accepts(Dict(
        'mail_update',
        Str('fromemail', validators=[Email()]),
        Str('fromname'),
        Str('outgoingserver'),
        Int('port'),
        Str('security', enum=['PLAIN', 'SSL', 'TLS']),
        Bool('smtp'),
        Str('user'),
        Str('pass', private=True),
        Dict('oauth',
             Str('client_id', required=True),
             Str('client_secret', required=True),
             Str('access_token', required=True),
             Str('refresh_token', required=True),
             Str('token_uri', required=True),
             null=True,
             private=True),
        register=True,
        update=True,
    ))
    async def do_update(self, data):
        """
        Update Mail Service Configuration.

        `fromemail` is used as a sending address which the mail server will use for sending emails.

        `outgoingserver` is the hostname or IP address of SMTP server used for sending an email.

        `security` is type of encryption desired.

        `smtp` is a boolean value which when set indicates that SMTP authentication has been enabled and `user`/`pass`
        are required attributes now.
        """
        config = await self.config()

        new = config.copy()
        new.update(data)
        new['security'] = new['security'].lower()  # Django Model compatibility

        verrors = ValidationErrors()

        if new['smtp'] and new['user'] == '':
            verrors.add(
                'mail_update.user',
                'This field is required when SMTP authentication is enabled',
            )

        if new['smtp'] and new['oauth']:
            verrors.add(
                'mail_update.oauth',
                'OAuth should be disabled when SMTP username/password authentication is enabled'
            )

        self.__password_verify(new['pass'], 'mail_update.pass', verrors)

        if verrors:
            raise verrors

        await self.middleware.call('datastore.update', 'system.email', config['id'], new, {'prefix': 'em_'})

        if new['oauth']:
            self.oauth_access_token = None

        return await self.config()

    def __password_verify(self, password, schema, verrors=None):
        if not password:
            return
        if verrors is None:
            verrors = ValidationErrors()
        # FIXME: smtplib does not support non-ascii password yet
        # https://github.com/python/cpython/pull/8938
        try:
            password.encode('ascii')
        except UnicodeEncodeError:
            verrors.add(
                schema,
                'Only plain text characters (7-bit ASCII) are allowed in passwords. '
                'UTF or composed characters are not allowed.'
            )
        return verrors

    @accepts(Dict(
        'mail_message',
        Str('subject', required=True),
        Str('text', required=True, max_length=None),
        Str('html', null=True, max_length=None),
        List('to', items=[Str('email')]),
        List('cc', items=[Str('email')]),
        Int('interval', null=True),
        Str('channel', null=True),
        Int('timeout', default=300),
        Bool('attachments', default=False),
        Bool('queue', default=True),
        Dict('extra_headers', additional_attrs=True),
        register=True,
    ), Ref('mail_update'))
    @job(pipes=['input'], check_pipes=False)
    def send(self, job, message, config):
        """
        Sends mail using configured mail settings.

        `text` will be formatted to HTML using Markdown and rendered using default E-Mail template.
        You can put your own HTML using `html`. If `html` is null, no HTML MIME part will be added to E-Mail.

        If `attachments` is true, a list compromised of the following dict is required
        via HTTP upload:
          - headers(list)
            - name(str)
            - value(str)
            - params(dict)
          - content (str)

        [
         {
          "headers": [
           {
            "name": "Content-Transfer-Encoding",
            "value": "base64"
           },
           {
            "name": "Content-Type",
            "value": "application/octet-stream",
            "params": {
             "name": "test.txt"
            }
           }
          ],
          "content": "dGVzdAo="
         }
        ]
        """

        if config:
            self.oauth_access_token = None

        product_name = self.middleware.call_sync('system.product_name')

        gc = self.middleware.call_sync('datastore.config', 'network.globalconfiguration')

        hostname = f'{gc["gc_hostname"]}.{gc["gc_domain"]}'

        message['subject'] = f'{product_name} {hostname}: {message["subject"]}'

        if 'html' in message and message['html'] is None:
            message.pop('html')
        elif 'html' not in message:
            lookup = TemplateLookup(
                directories=[os.path.join(os.path.dirname(os.path.realpath(__file__)), '../assets/templates')],
                module_directory="/tmp/mako/templates")

            tmpl = lookup.get_template('mail.html')

            message['html'] = tmpl.render(body=html.escape(message['text']).replace('\n', '<br>\n'))

        return self.send_raw(job, message, config)

    @accepts(Ref('mail_message'), Ref('mail_update'))
    @job(pipes=['input'], check_pipes=False)
    @private
    def send_raw(self, job, message, config):
        config = dict(self.middleware.call_sync('mail.config'), **config)

        if config['fromname']:
            from_addr = Header(config['fromname'], 'utf-8')
            try:
                config['fromemail'].encode('ascii')
            except UnicodeEncodeError:
                from_addr.append(f'<{config["fromemail"]}>', 'utf-8')
            else:
                from_addr.append(f'<{config["fromemail"]}>', 'ascii')
        else:
            try:
                config['fromemail'].encode('ascii')
            except UnicodeEncodeError:
                from_addr = Header(config['fromemail'], 'utf-8')
            else:
                from_addr = Header(config['fromemail'], 'ascii')

        interval = message.get('interval')
        if interval is None:
            interval = timedelta()
        else:
            interval = timedelta(seconds=interval)

        sw_name = self.middleware.call_sync('system.info')['version'].split('-', 1)[0]

        channel = message.get('channel')
        if not channel:
            channel = sw_name.lower()
        if interval > timedelta():
            channelfile = '/tmp/.msg.%s' % (channel)
            last_update = datetime.now() - interval
            try:
                last_update = datetime.fromtimestamp(os.stat(channelfile).st_mtime)
            except OSError:
                pass
            timediff = datetime.now() - last_update
            if (timediff >= interval) or (timediff < timedelta()):
                # Make sure mtime is modified
                # We could use os.utime but this is simpler!
                with open(channelfile, 'w') as f:
                    f.write('!')
            else:
                raise CallError('This message was already sent in the given interval')

        verrors = self.__password_verify(config['pass'], 'mail-config.pass')
        if verrors:
            raise verrors
        to = message.get('to')
        if not to:
            to = [
                self.middleware.call_sync(
                    'user.query', [('username', '=', 'root')], {'get': True}
                )['email']
            ]
            if not to[0]:
                raise CallError('Email address for root is not configured')

        if message.get('attachments'):
            job.check_pipe("input")

            def read_json():
                f = job.pipes.input.r
                data = b''
                i = 0
                while True:
                    read = f.read(1048576)  # 1MiB
                    if read == b'':
                        break
                    data += read
                    i += 1
                    if i > 50:
                        raise ValueError('Attachments bigger than 50MB not allowed yet')
                if data == b'':
                    return None
                return json.loads(data)

            attachments = read_json()
        else:
            attachments = None

        if 'html' in message or attachments:
            msg = MIMEMultipart()
            msg.preamble = 'This is a multi-part message in MIME format.'
            if 'html' in message:
                msg2 = MIMEMultipart('alternative')
                msg2.attach(MIMEText(message['text'], 'plain', _charset='utf-8'))
                msg2.attach(MIMEText(message['html'], 'html', _charset='utf-8'))
                msg.attach(msg2)
            if attachments:
                for attachment in attachments:
                    m = Message()
                    m.set_payload(attachment['content'])
                    for header in attachment.get('headers'):
                        m.add_header(header['name'], header['value'], **(header.get('params') or {}))
                    msg.attach(m)
        else:
            msg = MIMEText(message['text'], _charset='utf-8')

        msg['Subject'] = message['subject']

        msg['From'] = from_addr
        msg['To'] = ', '.join(to)
        if message.get('cc'):
            msg['Cc'] = ', '.join(message.get('cc'))
        msg['Date'] = formatdate()

        local_hostname = socket.gethostname()

        msg['Message-ID'] = "<%s-%s.%s@%s>" % (sw_name.lower(), datetime.utcnow().strftime("%Y%m%d.%H%M%S.%f"), base64.urlsafe_b64encode(os.urandom(3)), local_hostname)

        extra_headers = message.get('extra_headers') or {}
        for key, val in list(extra_headers.items()):
            # We already have "Content-Type: multipart/mixed" and setting "Content-Type: text/plain" like some scripts
            # do will break python e-mail module.
            if key.lower() == "content-type":
                continue

            if key in msg:
                msg.replace_header(key, val)
            else:
                msg[key] = val

        syslog.openlog(logoption=syslog.LOG_PID, facility=syslog.LOG_MAIL)
        try:
            server = self._get_smtp_server(config, message['timeout'], local_hostname=local_hostname)
            # NOTE: Don't do this.
            #
            # If smtplib.SMTP* tells you to run connect() first, it's because the
            # mailserver it tried connecting to via the outgoing server argument
            # was unreachable and it tried to connect to 'localhost' and barfed.
            # This is because FreeNAS doesn't run a full MTA.
            # else:
            #    server.connect()
            headers = '\n'.join([f'{k}: {v}' for k, v in msg._headers])
            syslog.syslog(f"sending mail to {', '.join(to)}\n{headers}")
            server.sendmail(from_addr.encode(), to, msg.as_string())
            server.quit()
        except Exception as e:
            # Don't spam syslog with these messages. They should only end up in the
            # test-email pane.
            # We are only interested in ValueError, not subclasses.
            if e.__class__ is ValueError:
                raise CallError(str(e))
            syslog.syslog(f'Failed to send email to {", ".join(to)}: {str(e)}')
            if isinstance(e, smtplib.SMTPAuthenticationError):
                raise CallError(f'Authentication error ({e.smtp_code}): {e.smtp_error}', errno.EAUTH)
            self.logger.warn('Failed to send email: %s', str(e), exc_info=True)
            if message['queue']:
                with MailQueue() as mq:
                    mq.append(msg)
            raise CallError(f'Failed to send email: {e}')
        return True

    def _get_smtp_server(self, config, timeout=300, local_hostname=None):
        if local_hostname is None:
            local_hostname = socket.gethostname()

        if not config['outgoingserver'] or not config['port']:
            # See NOTE below.
            raise ValueError('you must provide an outgoing mailserver and mail'
                             ' server port when sending mail')
        if config['security'] == 'SSL':
            server = smtplib.SMTP_SSL(
                config['outgoingserver'],
                config['port'],
                timeout=timeout,
                local_hostname=local_hostname)
        else:
            server = smtplib.SMTP(
                config['outgoingserver'],
                config['port'],
                timeout=timeout,
                local_hostname=local_hostname)
            if config['security'] == 'TLS':
                server.starttls()
        if config['smtp']:
            server.login(config['user'], config['pass'])
        elif config['oauth']:
            if self.oauth_access_token is None or time.monotonic() + 300 >= self.oauth_access_token_expires_at:
                session = OAuth2Session(config['oauth']['client_id'])
                access_token = session.refresh_token(config['oauth']['token_uri'],
                                                     config['oauth']['refresh_token'],
                                                     client_id=config['oauth']['client_id'],
                                                     client_secret=config['oauth']['client_secret'])
                self.oauth_access_token_expires_at = time.monotonic() + access_token['expires_in']
                self.oauth_access_token = access_token['access_token']

            auth_string = f'user={config["fromemail"]}\1auth=Bearer {self.oauth_access_token}\1\1'
            auth_string = base64.b64encode(auth_string.encode('ascii')).decode('ascii')
            server.docmd('AUTH', f'XOAUTH2 {auth_string}')

        return server

    @periodic(600, run_on_start=False)
    @private
    def send_mail_queue(self):

        with MailQueue() as mq:
            for queue in list(mq.queue):
                try:
                    config = self.middleware.call_sync('mail.config')
                    server = self._get_smtp_server(config)
                    server.sendmail(queue.message['From'].encode(), queue.message['To'].split(', '), queue.message.as_string())
                    server.quit()
                except Exception:
                    self.logger.debug('Sending message from queue failed', exc_info=True)
                    queue.attempts += 1
                    if queue.attempts >= mq.MAX_ATTEMPTS:
                        mq.queue.remove(queue)
                else:
                    mq.queue.remove(queue)
