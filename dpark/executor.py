#!/usr/bin/env python

# hook for virtualenv
# switch to the virtualenv where the executor belongs,
# replace all the path for modules
import sys, os.path
P = 'site-packages'
apath = os.path.abspath(__file__)
if P in apath:
    virltualenv = apath[:apath.index(P)]
    sysp = [p[:-len(P)] for p in sys.path if p.endswith(P)][0]
    if sysp != virltualenv:
        sys.path = [p.replace(sysp, virltualenv) for p in sys.path]

import logging
import os, sys, time
import signal
import os.path
import marshal
import cPickle
import multiprocessing
import threading
import SocketServer
import SimpleHTTPServer
import shutil
import socket
import urllib2
import platform
import gc

import zmq

import pymesos as mesos
import pymesos.mesos_pb2 as mesos_pb2

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dpark.util import compress, decompress, getproctitle, setproctitle, spawn
from dpark.serialize import marshalable
from dpark.accumulator import Accumulator
from dpark.schedule import Success, FetchFailed, OtherFailure
from dpark.env import env
from dpark.shuffle import LocalFileShuffle

logger = logging.getLogger("executor@%s" % socket.gethostname())

TASK_RESULT_LIMIT = 1024 * 256
DEFAULT_WEB_PORT = 5055
MAX_IDLE_TIME = 60

Script = ''

def reply_status(driver, task, status, data=None):
    update = mesos_pb2.TaskStatus()
    update.task_id.value = task.task_id.value
    update.state = status
    if data is not None:
        update.data = data
    driver.sendStatusUpdate(update)

def run_task(task_data):
    try:
        gc.disable()
        task, ntry = cPickle.loads(decompress(task_data))
        setproctitle('dpark worker %s: run task %s' % (Script, task))

        Accumulator.clear()
        result = task.run(ntry)
        accUpdate = Accumulator.values()

        if marshalable(result):
            flag, data = 0, marshal.dumps(result)
        else:
            flag, data = 1, cPickle.dumps(result, -1)
        data = compress(data)

        if len(data) > TASK_RESULT_LIMIT:
            path = LocalFileShuffle.getOutputFile(0, ntry, task.id, len(data))
            f = open(path, 'w')
            f.write(data)
            f.close()
            data = '/'.join([LocalFileShuffle.getServerUri()] + path.split('/')[-3:])
            flag += 2

        return mesos_pb2.TASK_FINISHED, cPickle.dumps((Success(), (flag, data), accUpdate), -1)
    except FetchFailed, e:
        return mesos_pb2.TASK_FAILED, cPickle.dumps((e, None, None), -1)
    except :
        import traceback
        msg = traceback.format_exc()
        return mesos_pb2.TASK_FAILED, cPickle.dumps((OtherFailure(msg), None, None), -1)
    finally:
        setproctitle('dpark worker: idle')
        gc.collect()
        gc.enable()

def init_env(args):
    setproctitle('dpark worker: idle')
    env.start(False, args)


class LocalizedHTTP(SimpleHTTPServer.SimpleHTTPRequestHandler):
    basedir = None
    def translate_path(self, path):
        out = SimpleHTTPServer.SimpleHTTPRequestHandler.translate_path(self, path)
        return self.basedir + '/' + os.path.relpath(out)

    def log_message(self, format, *args):
        pass

def startWebServer(path):
    # check the default web server
    if not os.path.exists(path):
        os.makedirs(path)
    testpath = os.path.join(path, 'test')
    with open(testpath, 'w') as f:
        f.write(path)
    default_uri = 'http://%s:%d/%s' % (socket.gethostname(), DEFAULT_WEB_PORT,
            os.path.basename(path))
    try:
        data = urllib2.urlopen(default_uri + '/' + 'test').read()
        if data == path:
            return default_uri
    except IOError, e:
        pass

    logger.warning("default webserver at %s not available", DEFAULT_WEB_PORT)
    LocalizedHTTP.basedir = os.path.dirname(path)
    ss = SocketServer.TCPServer(('0.0.0.0', 0), LocalizedHTTP)
    spawn(ss.serve_forever)
    uri = "http://%s:%d/%s" % (socket.gethostname(), ss.server_address[1],
            os.path.basename(path))
    return uri

def forword(fd, addr, prefix=''):
    f = os.fdopen(fd, 'r')
    ctx = zmq.Context()
    out = [None]
    buf = []
    def send(buf):
        if not out[0]:
            out[0] = ctx.socket(zmq.PUSH)
            out[0].connect(addr)
        out[0].send(prefix+''.join(buf))

    while True:
        try:
            line = f.readline()
            if not line: break
            buf.append(line)
            if line.endswith('\n'):
                send(buf)
                buf = []
        except IOError:
            break
    if buf:
        send(buf)
    if out[0]:
        out[0].close()
    f.close()
    ctx.shutdown()

def start_forword(addr, prefix, oldfile):
    rfd, wfd = os.pipe()
    t = spawn(forword, rfd, addr, prefix)
    newfile = os.fdopen(wfd, 'w', 0)
    return t, newfile

def get_pool_memory(pool):
    try:
        import psutil
        p = psutil.Process(pool._pool[0].pid)
        return p.get_memory_info()[0] >> 20
    except Exception:
        return 0

def get_task_memory(task):
    for r in task.resources:
        if r.name == 'mem':
            return r.scalar.value
    logger.error("no memory in resource: %s", task.resources)
    return 100 # 100M

def safe(f):
    def _(self, *a, **kw):
        with self.lock:
            r = f(self, *a, **kw)
        return r
    return _
            
def setup_cleaner_process(workdir):
    ppid = os.getpid()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        pid = os.fork()
        if pid == 0:
            try:
                import psutil
            except ImportError:
                os._exit(1)
            try: 
                setproctitle('dpark cleaner %s wait(%d)' % (workdir, ppid))
                psutil.Process(ppid).wait()
                os.killpg(ppid, signal.SIGKILL) # kill workers
            except Exception, e:
                pass # make sure to exit
            finally:
                setproctitle('dpark cleaning %s ' % workdir)
                for d in workdir:
                    while os.path.exists(d):
                        try: shutil.rmtree(d, True)
                        except: pass
        os._exit(0)
    os.wait()

class MyExecutor(mesos.Executor):
    def __init__(self):
        self.workdir = []
        self.idle_workers = []
        self.busy_workers = {}
        self.lock = threading.RLock()

    def check_memory(self, driver):
        try:
            import psutil
        except ImportError:
            logger.error("no psutil module")
            return

        mem_limit = {}

        while True:
            self.lock.acquire()

            for tid, (task, pool) in self.busy_workers.items():
                pid = pool._pool[0].pid
                try:
                    p = psutil.Process(pid)
                    rss = p.get_memory_info()[0] >> 20
                except psutil.error.Error, e:
                    logger.error("worker process %d of task %s is dead: %s", pid, tid, e)
                    reply_status(driver, task, mesos_pb2.TASK_LOST)
                    self.busy_workers.pop(tid)
                    continue

                if p.status == psutil.STATUS_ZOMBIE or not p.is_running():
                    logger.error("worker process %d of task %s is zombie", pid, tid)
                    reply_status(driver, task, mesos_pb2.TASK_LOST)
                    self.busy_workers.pop(tid)
                    continue

                offered = get_task_memory(task)
                if not offered:
                    continue
                if rss > offered * 1.5:
                    logger.warning("task %s used too much memory: %dMB > %dMB * 1.5, kill it. "
                            + "use -M argument or taskMemory to request more memory.", tid, rss, offered)
                    reply_status(driver, task, mesos_pb2.TASK_KILLED)
                    self.busy_workers.pop(tid)
                    pool.terminate()
                elif rss > offered * mem_limit.get(tid, 1.0):
                    logger.debug("task %s used too much memory: %dMB > %dMB, "
                            + "use -M to request or taskMemory for more memory", tid, rss, offered)
                    mem_limit[tid] = rss / offered + 0.1

            now = time.time()
            n = len([1 for t, p in self.idle_workers if t + MAX_IDLE_TIME < now])
            if n:
                for _, p in self.idle_workers[:n]:
                    p.terminate()
                self.idle_workers = self.idle_workers[n:]

            self.lock.release()

            time.sleep(1) 

    @safe
    def registered(self, driver, executorInfo, frameworkInfo, slaveInfo):
        try:
            global Script
            Script, cwd, python_path, osenv, self.parallel, out_logger, err_logger, logLevel, args = marshal.loads(executorInfo.data)
            self.init_args = args
            try:
                os.chdir(cwd)
            except OSError:
                driver.sendFrameworkMessage("switch cwd failed: %s not exists!" % cwd)
            sys.path = python_path
            os.environ.update(osenv)
            prefix = '[%s] ' % socket.gethostname()
            self.outt, sys.stdout = start_forword(out_logger, prefix, sys.stdout)
            self.errt, sys.stderr = start_forword(err_logger, prefix, sys.stderr)
            logging.basicConfig(format='%(asctime)-15s [%(levelname)s] [%(name)-9s] %(message)s', level=logLevel)

            self.workdir = args['WORKDIR']
            root = os.path.dirname(self.workdir[0])
            if not os.path.exists(root):
                os.mkdir(root)
                os.chmod(root, 0777) # because umask
            args['SERVER_URI'] = startWebServer(self.workdir[0])
            if 'MESOS_SLAVE_PID' in os.environ: # make unit test happy
                setup_cleaner_process(self.workdir)

            spawn(self.check_memory, driver)

            logger.debug("executor started at %s", slaveInfo.hostname)

        except Exception, e:
            import traceback
            msg = traceback.format_exc()
            logger.error("init executor failed: %s", msg)
            raise

    def get_idle_worker(self):
        try:
            return self.idle_workers.pop()[1]
        except IndexError:
            p = multiprocessing.Pool(1, init_env, [self.init_args])
            p.done = 0
            return p

    @safe
    def launchTask(self, driver, task):
        try:
            reply_status(driver, task, mesos_pb2.TASK_RUNNING)
            logging.debug("launch task %s", task.task_id.value)

            pool = self.get_idle_worker()
            self.busy_workers[task.task_id.value] = (task, pool)

            def callback((state, data)):
                with self.lock:
                    if task.task_id.value not in self.busy_workers:
                        return
                    _, pool = self.busy_workers.pop(task.task_id.value)
                    pool.done += 1
                    reply_status(driver, task, state, data)
                    self.idle_workers.append((time.time(), pool))
        
            pool.apply_async(run_task, [task.data], callback=callback)

        except Exception, e:
            import traceback
            msg = traceback.format_exc()
            reply_status(driver, task, mesos_pb2.TASK_LOST, msg)
            return

    @safe
    def killTask(self, driver, taskId):
        if taskId.value in self.busy_workers:
            task, pool = self.busy_workers.pop(taskId.value)
            reply_status(driver, task, mesos_pb2.TASK_KILLED)
            pool.terminate()

    @safe
    def shutdown(self, driver=None):
        def terminate(p):
            try:
                for pi in p._pool:
                    os.kill(pi.pid, signal.SIGKILL)
            except Exception, e:
                pass
        for _, p in self.idle_workers:
            terminate(p)
        for _, p in self.busy_workers.itervalues():
            terminate(p)

        # clean work files
        for d in self.workdir:
            try: shutil.rmtree(d, True)
            except: pass
        
        sys.stdout.close()
        sys.stderr.close()
        os.close(1)
        os.close(2)
        self.outt.join()
        self.errt.join()
        
def run():
    executor = MyExecutor()
    driver = mesos.MesosExecutorDriver(executor)
    driver.run()

if __name__ == '__main__':
    run()
