import time 
import logging

TASK_STARTING = 0
TASK_RUNNING  = 1
TASK_FINISHED = 2
TASK_FAILED   = 3
TASK_KILLED   = 4
TASK_LOST     = 5

class Job:
    def __init__(self):
        self.id = self.newJobId()

    def slaveOffer(self, s, availableCpus):
        raise NotImplementedError

    def statusUpdate(self, t):
        raise NotImplementedError

    def error(self, code, message):
        raise NotImplementedError
    
    nextJobId = 0
    nextTaskId = 0

    @classmethod
    def newJobId(cls):
        cls.nextJobId += 1
        return cls.nextJobId

LOCALITY_WAIT = 5
MAX_TASK_FAILURES = 4
CPUS_PER_TASK = 1

# A Job that runs a set of tasks with no interdependencies.
class SimpleJob(Job):

    def __init__(self, sched, tasks):
        Job.__init__(self)
        self.sched = sched
        self.tasks = tasks

        self.launched = [False] * len(tasks)
        self.finished = [False] * len(tasks)
        self.numFailures = [0] * len(tasks)
        self.tidToIndex = {}

        self.lastPreferredLaunchTime = time.time()

        self.pendingTasksForHost = {}
        self.pendingTasksWithNoPrefs = []
        self.allPendingTasks = []

        self.failed = False
        self.causeOfFailure = ""

        for i in range(len(tasks)):
            self.addPendingTask(i)

    @property
    def numTasks(self):
        return len(self.tasks)

    @property
    def tasksLaunched(self):
        return self.launched.count(True)

    @property
    def tasksFinished(self):
        return self.finished.count(True)

    def addPendingTask(self, i):
        loc = self.tasks[i].preferredLocations()
        if not loc:
            self.pendingTasksWithNoPrefs.append(i)
        else:
            for host in loc:
                self.pendingTasksForHost.setdefault(host, []).append(i)
        self.allPendingTasks.append(i)

    def getPendingTasksForHost(self, host):
        return self.pendingTasksForHost.setdefault(host, [])

    def findTaskFromList(self, l):
        for i in l:
            if not self.launched[i] and not self.finished[i]:
                return i

    def findTask(self, host, localOnly):
        localTask = self.findTaskFromList(self.getPendingTasksForHost(host))
        if localTask is not None:
            return localTask
        noPrefTask = self.findTaskFromList(self.pendingTasksWithNoPrefs)
        if noPrefTask is not None:
            return noPrefTask
#        raise self.pendingTasksWithNoPrefs
        if not localOnly:
            return self.findTaskFromList(self.allPendingTasks)

    def isPreferredLocation(self, task, host):
        locs = task.preferredLocations()
        return host in locs or not locs

    # Respond to an offer of a single slave from the scheduler by finding a task
    def slaveOffer(self, host, availableCpus): 
        if self.tasksLaunched >= self.numTasks or availableCpus < CPUS_PER_TASK:
            return
        now = time.time()
        localOnly = (now - self.lastPreferredLaunchTime < LOCALITY_WAIT)
        i =  self.findTask(host, localOnly)
        if i is not None:
            task = self.tasks[i]
            preferred = self.isPreferredLocation(task, host)
            prefStr = preferred and "preferred" or "non-preferred"
            logging.info("Starting task %d:%d as TID %s on slave %s (%s)", 
                self.id, i, task, host, prefStr)
            self.tidToIndex[task.id] = i
            self.launched[i] = True
            if preferred:
                self.lastPreferredLaunchTime = now
            return task
        logging.info("no task found %s", localOnly)

    def statusUpdate(self, tid, status, reason=None, result=None, update=None):
        logging.info("job status update %s %s %s %s %s", tid, status,
            result, update, reason)
        if status == TASK_FINISHED:
            self.taskFinished(tid, result, update)
        elif status in (TASK_LOST, 
                    TASK_FAILED, TASK_KILLED):
            self.taskLost(tid, status, reason)

    def taskFinished(self, tid, result, update):
        i = self.tidToIndex[tid]
        if not self.finished[i]:
            self.finished[i] = True
            logging.error("Finished TID %s (progress: %d/%d)", tid, self.tasksFinished, self.numTasks)
            from schedule import Success
            self.sched.taskEnded(self.tasks[i], Success(), result, update)
            if self.tasksFinished == self.numTasks:
                self.sched.jobFinished(self)
        else:
            logging.warning("Ignoring task-finished event for TID %d because task %d is already finished", tid, i)

    def taskLost(self, tid, status, reason):
        index = self.tidToIndex[tid]
        if not self.finished[index]:
            logging.warning("Lost TID %s (task %d:%d)", tid, self.id, index)
            self.launched[index] = False
            from schedule import FetchFailed
            if isinstance(reason, FetchFailed):
                logging.warning("Loss was due to fetch failure from %s", reason.serverUri)
                self.sched.taskEnded(self.tasks[index], reason, None, None)
                self.finished[index] = True
                if self.tasksFinished == self.numTasks:
                    self.sched.jobFinished(self)
                return
            logging.warning("re-enqueue the task as pending for a max number of retries")
            self.addPendingTask(index)
            if status in (TASK_FAILED, TASK_LOST):
                self.numFailures[index] += 1
                if self.numFailures[index] > MAX_TASK_FAILURES:
                    logging.error("Task %d failed more than %d times; aborting job", index, MAX_TASK_FAILURES)
                    self.abort("Task %d failed more than %d times" % (index, MAX_TASK_FAILURES))

        else:
            logging.warning("Ignoring task-lost event for TID %d because task %d is already finished")

    def error(self, code, message):
        self.abort("Mesos error: %s (error code: %d)" % (message, code))

    def abort(self, message):
        self.failed = True
        self.causeOfFailure = message
        self.sched.jobFinished(self)