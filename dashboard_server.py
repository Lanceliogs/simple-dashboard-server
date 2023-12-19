# -*- coding: utf-8 -*-

import socket
import select
import os, sys
import datetime
import subprocess
import json
import argparse
import logging
import logging.handlers
import re
import signal

# determine if application is a script file or frozen exe
global application_dir_path
if getattr(sys, 'frozen', False):
    application_dir_path = os.path.dirname(sys.executable)
elif __file__:
    application_dir_path = os.path.dirname(__file__)

# Interrupt signal to close the program on terminate
global interrupted
def signal_handler(signal, frame):
    global interrupted
    interrupted = True

# Child process class wrapper
class ChildProcess():
    def __init__(self, *,
                 description: str = '',
                 args: list[str] = [],
                 cwd: str = '.') -> None:
        
        self.args = args
        self.cwd = cwd

        if description:
            self.description = description
        else:
            self.description = args[0] if len(args) > 0 else 'INVALID'
        
        self.pid = 0
        pass

    def start_detached(self) -> int:
        proc = subprocess.Popen(creationflags=subprocess.DETACHED_PROCESS,
                                args=self.args,
                                cwd=self.cwd)
        self.pid = proc.pid
        return self.pid
    
    def pid(self):
        return self.pid

###
# Some nice hack to reload the script
global touched
def restart_server():
    args = []
    if __file__.endswith('.exe'):
        args = [__file__]
    elif __file__.endswith('.py') or __file__.endswith('.pyw'):
        args = ['pythonw.exe', __file__]
    else:
        pass
    subprocess.Popen(creationflags=subprocess.DETACHED_PROCESS,
                     args=args,
                     cwd=os.getcwd())

# Default configuration file is next to the script
default_cfg_file = os.path.join(application_dir_path, 'config.json')
default_log_file = os.path.join(application_dir_path, 'dashboard.log')

parser = argparse.ArgumentParser(prog='### Enovasense Dashboard server for machines ###',
                                 formatter_class=argparse.RawTextHelpFormatter,
                                 description="""
###############################################################
### ENOVASENSE DASHBOARD SERVER - CONFIGURATION FILE HELPER ###

This tool is a little TCP server that applies commands when it receives some simple ascii messages.
               
The commands are fully configurable in a json configuration file.
The configuration file should contain the following keys:
                
    - host: a string containing the ip address of the host to listen to. 0.0.0.0 works for any host.
    - port: an int containing the port of the server
                
    - log_file: the path of the log file
    - log_rotate_size: the maximum size of the log file (then it will be rotated)
    - log_backup_count: how many files will we have

    - touch_reload: Path of a file. If this file exists,
      the server will restart and reload the configuration file.

    - commands: an array containing the list of the commands.

Each command has he following keys:
    - token: The string that will call this command when received.
    - args: An array of strings.
      The first element is the program, the others are the arguments.
    - cwd: The working directory of the process.

###############################################################
""")

parser.add_argument('--conf', type=str, default=default_cfg_file, help='Path of the configuration file of the server.')
args = parser.parse_args()

###
# Config file
cfg_file = os.path.abspath(args.conf)
if not os.path.exists(cfg_file):
    print (f'INVALID ARGUMENT: Config file does not exist: {cfg_file}', file=sys.stderr)
    exit(1)
with open(cfg_file, 'r', encoding='utf-8') as f:
    cfg = json.load(f)

###
# TCP server config
host: str = str(cfg.get('host', '0.0.0.0'))
port: int = int(cfg.get('port', 36001))

###
# Log file path
logfile: str = cfg.get('log_file', default_log_file)
rotating_backup_count = cfg.get('log_backup_count', 10)
rotate_size = cfg.get('log_rotate_size_KB', 1024) * 1024

###
# Commands list info
PROCESSES_MAPPING: dict = {}
for cmd in cfg.get('commands'):
    token = cmd.get('token')
    cmd_args: list[str] = [arg for arg in cmd.get('args', [])]
    cmd_cwd = cfg.get('cwd')
    PROCESSES_MAPPING.update({token: ChildProcess(args=cmd_args, cwd=cmd_cwd)})

###
# Configure and start TCP socket
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((host, port))
server.listen(5)

socks = [server]
addresses = {}

# Create and configure logger
logger = logging.getLogger("Dashboard Server")
logger.setLevel(logging.INFO)
    
# add a rotating handler
file_hdlr = logging.handlers.RotatingFileHandler(logfile,
                                               maxBytes=rotate_size,
                                               backupCount=rotating_backup_count)
formatter = logging.Formatter(fmt='[%(asctime)s.%(msecs)03d] %(message)s', datefmt='%F %T')
file_hdlr.setFormatter(formatter)
logger.addHandler(file_hdlr)

stream_hdlr = logging.StreamHandler(sys.stdout)
stream_hdlr.setFormatter(formatter)
logger.addHandler(stream_hdlr)

###
# touch-reload
touch_reload_file = cfg.get("touch_reload", None)
if touch_reload_file:
    touch_reload_file = os.path.join(os.path.dirname(__file__), touch_reload_file)
    if os.path.exists(touch_reload_file):
        os.remove(touch_reload_file)
        logger.info('Touch-reload file removed')

###
# Let's go
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

logger.info(f'New session - Listening on {host}:{port}...')

###
# Interrupt flags
touched = False
interrupted = False

###
# Main program loop
while True:

    # Interrupt management
    if interrupted:
        logger.info("SIGINT/SIGTERM received. Closing the session.")
        break

    # Touch-reload
    if os.path.exists(touch_reload_file):
        touched = True
        logger.info("Touch-reload invoked. Reloading...")
        break

    readables, _, _  = select.select(socks, [], [], 0.1)

    for s in readables:

        # New connection
        if s is server:
            sock, address = s.accept()
            if not sock in socks:
                socks.append(sock)
                addresses.update({sock: address})

        # Read from already connected socket
        else:
            # Trying to read from the readable socket
            try:
                buf = s.recv(1024)
            except socket.error as error:
                buf = bytes()
                logging.info(f"[{addresses.get(s, 0)}] Socket recv error: {os.strerror(error.errno)}")

            if not buf:
                s.close()
                logger.info(f"Connection closed: {addresses.get(s, 0)}")
                if s in socks:
                    socks.remove(s)
                    addresses.pop(s)
                continue

            # Trying to decode message in utf-8
            message: str = ""
            try:
                message = buf.decode(encoding='ascii')
            except:
                logger.info("Couldn't decode message in ascii. Some chars are not supported.")
                continue
                
            tokens = [token.strip('\r ') for token in message.split('\n') if token.strip('\r ')]
            logger.info(f'[{addresses.get(s, 0)}] RECV: [{", ".join(tokens)}]')

            # Is it valid?
            for token in tokens:
                if token not in PROCESSES_MAPPING:
                    logger.info(f'Unrecognized token: {token}')
                    continue
                # Start detached
                proc = PROCESSES_MAPPING.get(token)
                pid = proc.start_detached()
                logger.info(f'Started {proc.description} at PID: {pid}')

# Close everything
for sock in socks:
    sock.close()

if not touched:
    exit(0)

restart_server()
exit(0)
