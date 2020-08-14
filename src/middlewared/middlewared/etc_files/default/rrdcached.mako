<%
    if not middleware.call_sync('reporting.setup'):
        # Let's exit this if setup related disk operations fail
        middleware.logger.error('Rrdcached configuration file could not be generated')
        raise FileShouldNotExist()
%>
# Full path to daemon
DAEMON=/usr/bin/rrdcached

# Where journal files are placed.  If left unset, journaling will
# be disabled.
JOURNAL_PATH=/var/lib/rrdcached/journal/

# FHS standard placement for process ID file.
PIDFILE=/var/run/rrdcached.pid

# FHS standard placement for local control socket.
SOCKFILE=/var/run/rrdcached.sock
