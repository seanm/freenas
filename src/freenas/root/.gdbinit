#set debug-file-directory /mnt/tank/world
add-auto-load-safe-path /usr/local/lib
define init_python
python
sys.path.append('/usr/local/share/python-gdb')
import libpython
end
end
