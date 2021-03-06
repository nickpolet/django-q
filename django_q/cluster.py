# Future
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import range

from future import standard_library

standard_library.install_aliases()

# Standard
import importlib
import os
import signal
import socket
import sys
import ast
from time import sleep
from multiprocessing import Queue, Event, Process, Value, current_process

try:
    import cPickle as pickle
except ImportError:
    import pickle

# external
import arrow

# Django
from django.core import signing
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

# Local
from .conf import Conf, redis_client, logger
from .models import Task, Success, Schedule
from .monitor import Status, Stat
from .tasks import SignedPackage, async


class Cluster(object):
    def __init__(self, list_key=Conf.Q_LIST):
        try:
            redis_client.ping()
        except Exception as e:
            logger.exception(e)
            raise e
        self.sentinel = None
        self.stop_event = None
        self.start_event = None
        self.pid = current_process().pid
        self.host = socket.gethostname()
        self.list_key = list_key
        self.timeout = Conf.TIMEOUT
        signal.signal(signal.SIGTERM, self.sig_handler)
        signal.signal(signal.SIGINT, self.sig_handler)

    def start(self):
        # This is just for PyCharm to not crash. Ignore it.
        if not hasattr(sys.stdin, 'close'):
            def dummy_close():
                pass

            sys.stdin.close = dummy_close
        # Start Sentinel
        self.stop_event = Event()
        self.start_event = Event()
        self.sentinel = Process(target=Sentinel, args=(self.stop_event, self.start_event, self.list_key, self.timeout))
        self.sentinel.start()
        logger.info(_('Q Cluster-{} starting.').format(self.pid))
        while not self.start_event.is_set():
            sleep(0.2)
        return self.pid

    def stop(self):
        if not self.sentinel.is_alive():
            return False
        logger.info(_('Q Cluster-{} stopping.').format(self.pid))
        self.stop_event.set()
        self.sentinel.join()
        logger.info(_('Q Cluster-{} has stopped.').format(self.pid))
        self.start_event = None
        self.stop_event = None
        return True

    def sig_handler(self, signum, frame):
        logger.debug(_('{} got signal {}').format(current_process().name, Conf.SIGNAL_NAMES.get(signum, 'UNKNOWN')))
        self.stop()

    @property
    def stat(self):
        if self.sentinel:
            return Stat.get(self.pid)
        return Status(self.pid)

    @property
    def is_starting(self):
        return self.stop_event and self.start_event and not self.start_event.is_set()

    @property
    def is_running(self):
        return self.stop_event and self.start_event and self.start_event.is_set()

    @property
    def is_stopping(self):
        return self.stop_event and self.start_event and self.start_event.is_set() and self.stop_event.is_set()

    @property
    def has_stopped(self):
        return self.start_event is None and self.stop_event is None and self.sentinel


class Sentinel(object):
    def __init__(self, stop_event, start_event, list_key=Conf.Q_LIST, timeout=Conf.TIMEOUT, start=True):
        # Make sure we catch signals for the pool
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.pid = current_process().pid
        self.parent_pid = os.getppid()
        self.name = current_process().name
        self.list_key = list_key
        self.r = redis_client
        self.reincarnations = 0
        self.tob = timezone.now()
        self.stop_event = stop_event
        self.start_event = start_event
        self.pool_size = Conf.WORKERS
        self.pool = []
        self.timeout = timeout
        self.task_queue = Queue()
        self.result_queue = Queue()
        self.event_out = Event()
        self.monitor = Process()
        self.pusher = Process()
        if start:
            self.start()

    def start(self):
        self.spawn_cluster()
        self.guard()

    def status(self):
        if not self.start_event.is_set() and not self.stop_event.is_set():
            return Conf.STARTING
        elif self.start_event.is_set() and not self.stop_event.is_set():
            if self.result_queue.qsize() == 0 and self.task_queue.qsize() == 0:
                return Conf.IDLE
            return Conf.WORKING
        elif self.stop_event.is_set() and self.start_event.is_set():
            if self.monitor.is_alive() or self.pusher.is_alive() or len(self.pool) > 0:
                return Conf.STOPPING
            return Conf.STOPPED

    def spawn_process(self, target, *args):
        """
        :type target: function or class
        """
        # This is just for PyCharm to not crash. Ignore it.
        if not hasattr(sys.stdin, 'close'):
            def dummy_close():
                pass

            sys.stdin.close = dummy_close
        p = Process(target=target, args=args)
        p.daemon = True
        if target == worker:
            p.timer = args[2]
            self.pool.append(p)
        p.start()
        return p

    def spawn_pusher(self):
        return self.spawn_process(pusher, self.task_queue, self.event_out, self.list_key, self.r)

    def spawn_worker(self):
        self.spawn_process(worker, self.task_queue, self.result_queue, Value('b', -1))

    def spawn_monitor(self):
        return self.spawn_process(monitor, self.result_queue)

    def reincarnate(self, process):
        """
        :param process: the process to reincarnate
        :type process: Process
        """
        if process == self.monitor:
            self.monitor = self.spawn_monitor()
            logger.error(_("reincarnated monitor {} after sudden death").format(process.name))
        elif process == self.pusher:
            self.pusher = self.spawn_pusher()
            logger.error(_("reincarnated pusher {} after sudden death").format(process.name))
        else:
            self.pool.remove(process)
            self.spawn_worker()
            if self.timeout and int(process.timer.value) >= self.timeout:
                # only need to terminate on timeout, otherwise we risk destabilizing the queues
                process.terminate()
                logger.warn(_("reincarnated worker {} after timeout").format(process.name))
            elif int(process.timer.value) == -2:
                logger.info(_("recycled worker {}").format(process.name))
            else:
                logger.error(_("reincarnated worker {} after death").format(process.name))

        self.reincarnations += 1

    def spawn_cluster(self):
        self.pool = []
        Stat(self).save()
        for i in range(self.pool_size):
            self.spawn_worker()
        self.monitor = self.spawn_monitor()
        self.pusher = self.spawn_pusher()

    def guard(self):
        logger.info(_('{} guarding cluster at {}').format(current_process().name, self.pid))
        self.start_event.set()
        Stat(self).save()
        logger.info(_('Q Cluster-{} running.').format(self.parent_pid))
        scheduler(list_key=self.list_key)
        counter = 0
        # Guard loop. Runs at least once
        while not self.stop_event.is_set() or not counter:
            # Check Workers
            for p in self.pool:
                # Are you alive?
                if not p.is_alive() or (self.timeout and int(p.timer.value) >= self.timeout):
                    self.reincarnate(p)
                    continue
                # Increment timer if work is being done
                if p.timer.value >= 0:
                    p.timer.value += 1
            # Check Monitor
            if not self.monitor.is_alive():
                self.reincarnate(self.monitor)
            # Check Pusher
            if not self.pusher.is_alive():
                self.reincarnate(self.pusher)
            # Call scheduler once a minute (or so)
            counter += 1
            if counter > 120:
                counter = 0
                scheduler(list_key=self.list_key)
            # Save current status
            Stat(self).save()
            sleep(0.5)
        self.stop()

    def stop(self):
        Stat(self).save()
        name = current_process().name
        logger.info('{} stopping cluster processes'.format(name))
        # Stopping pusher
        self.event_out.set()
        # Wait for it to stop
        while self.pusher.is_alive():
            sleep(0.2)
            Stat(self).save()
        # Put poison pills in the queue
        for _ in range(len(self.pool)):
            self.task_queue.put('STOP')
        self.task_queue.close()
        # wait for the task queue to empty
        self.task_queue.join_thread()
        # Wait for all the workers to exit
        while len(self.pool):
            for p in self.pool:
                if not p.is_alive():
                    self.pool.remove(p)
            sleep(0.2)
            Stat(self).save()
        # Finally stop the monitor
        self.result_queue.put('STOP')
        self.result_queue.close()
        # Wait for the result queue to empty
        self.result_queue.join_thread()
        logger.info('{} waiting for the monitor.'.format(name))
        # Wait for everything to close or time out
        count = 0
        if not self.timeout:
            self.timeout = 30
        while self.status() == Conf.STOPPING and count < self.timeout * 5:
            sleep(0.2)
            Stat(self).save()
            count += 1
        # Final status
        Stat(self).save()


def pusher(task_queue, e, list_key=Conf.Q_LIST, r=redis_client):
    """
    Pulls tasks of the Redis List and puts them in the task queue
    :type task_queue: multiprocessing.Queue
    :type e: multiprocessing.Event
    :type list_key: str
    """
    logger.info(_('{} pushing tasks at {}').format(current_process().name, current_process().pid))
    while True:
        try:
            task = r.blpop(list_key, 1)
        except Exception as e:
            logger.error(e)
            # redis probably crashed. Let the sentinel handle it.
            sleep(10)
            break
        if task:
            task = task[1]
            task_queue.put(task)
            logger.debug(_('queueing {}').format(task))
        if e.is_set():
            break
    logger.info(_("{} stopped pushing tasks").format(current_process().name))


def monitor(result_queue):
    """
    Gets finished tasks from the result queue and saves them to Django
    :type result_queue: multiprocessing.Queue
    """
    name = current_process().name
    logger.info(_("{} monitoring at {}").format(name, current_process().pid))
    for task in iter(result_queue.get, 'STOP'):
        save_task(task)
        if task['success']:
            logger.info(_("Processed [{}]").format(task['name']))
        else:
            logger.error(_("Failed [{}] - {}").format(task['name'], task['result']))
    logger.info(_("{} stopped monitoring results").format(name))


def worker(task_queue, result_queue, timer):
    """
    Takes a task from the task queue, tries to execute it and puts the result back in the result queue
    :type task_queue: multiprocessing.Queue
    :type result_queue: multiprocessing.Queue
    :type timer: multiprocessing.Value
    """
    name = current_process().name
    logger.info(_('{} ready for work at {}').format(name, current_process().pid))
    task_count = 0
    # Start reading the task queue
    for pack in iter(task_queue.get, 'STOP'):
        result = None
        timer.value = -1  # Idle
        task_count += 1
        # unpickle the task
        try:
            task = SignedPackage.loads(pack)
        except (TypeError, signing.BadSignature) as e:
            logger.error(e)
            continue
        # Get the function from the task
        logger.info(_('{} processing [{}]').format(name, task['name']))
        f = task['func']
        # if it's not an instance try to get it from the string
        if not callable(task['func']):
            try:
                module, func = f.rsplit('.', 1)
                m = importlib.import_module(module)
                f = getattr(m, func)
            except (ValueError, ImportError, AttributeError) as e:
                result = (e, False)
        # We're still going
        if not result:
            # execute the payload
            timer.value = 0  # Busy
            try:
                res = f(*task['args'], **task['kwargs'])
                result = (res, True)
            except Exception as e:
                result = (e, False)
        # Process result
        task['result'] = result[0]
        task['success'] = result[1]
        task['stopped'] = timezone.now()
        result_queue.put(task)
        timer.value = -1  # Idle
        # Recycle
        if task_count == Conf.RECYCLE:
            timer.value = -2
            break
    logger.info(_('{} stopped doing work').format(name))


def save_task(task):
    """
    Saves the task package to Django
    """
    # SAVE LIMIT < 0 : Don't save success
    if Conf.SAVE_LIMIT < 0 and task['success']:
        return
    # SAVE LIMIT > 0: Prune database, SAVE_LIMIT 0: No pruning
    if task['success'] and 0 < Conf.SAVE_LIMIT < Success.objects.count():
        Success.objects.first().delete()

    try:
        Task.objects.create(id=task['id'],
                            name=task['name'],
                            func=task['func'],
                            hook=task['hook'],
                            args=task['args'],
                            kwargs=task['kwargs'],
                            started=task['started'],
                            stopped=task['stopped'],
                            result=task['result'],
                            success=task['success'])
    except Exception as e:
        logger.error(e)


def scheduler(list_key=Conf.Q_LIST):
    """
    Creates a task from a schedule at the scheduled time and schedules next run
    """
    for s in Schedule.objects.exclude(repeats=0).filter(next_run__lt=timezone.now()):
        args = ()
        kwargs = {}
        # get args, kwargs and hook
        if s.kwargs:
            try:
                # eval should be safe here cause dict()
                kwargs = eval('dict({})'.format(s.kwargs))
            except SyntaxError:
                kwargs = {}
        if s.args:
            args = ast.literal_eval(s.args)
            # single value won't eval to tuple, so:
            if type(args) != tuple:
                args = (args,)
        if s.hook:
            kwargs['hook'] = s.hook
        # set up the next run time
        if not s.schedule_type == s.ONCE:
            next_run = arrow.get(s.next_run)
            if s.schedule_type == s.HOURLY:
                next_run = next_run.replace(hours=+1)
            elif s.schedule_type == s.DAILY:
                next_run = next_run.replace(days=+1)
            elif s.schedule_type == s.WEEKLY:
                next_run = next_run.replace(weeks=+1)
            elif s.schedule_type == s.MONTHLY:
                next_run = next_run.replace(months=+1)
            elif s.schedule_type == s.QUARTERLY:
                next_run = next_run.replace(months=+3)
            elif s.schedule_type == s.YEARLY:
                next_run = next_run.replace(years=+1)
            s.next_run = next_run.datetime
            s.repeats += -1
        else:
            s.repeats = 0
        # send it to the cluster
        kwargs['list_key'] = list_key
        s.task = async(s.func, *args, **kwargs)
        if not s.task:
            logger.error(_('{} failed to create a task from schedule {} [{}]').format(current_process().name, s.id), s.func)
        else:
            logger.info(_('{} created a task from schedule {} [{}]').format(current_process().name, s.id, s.func))
        s.save()
