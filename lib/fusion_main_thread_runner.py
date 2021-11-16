"""
This module defines a class named FusionMainThreadRunner, which can be used to run an arbitrary closure in the main 
Fusion thread.
"""

import adsk.core
import adsk
import adsk.fusion
import logging
import queue
import uuid
import sys
import threading
import json

from typing import Optional, Callable, Any

_logger = logging.getLogger(__name__)
_logger.propagate = False

class FusionMainThreadRunner(object):
    def __init__(self,
        logger: Optional[logging.Logger] = _logger
    ):
        self._app : adsk.core.Application = adsk.core.Application.get()
        self._logger = logger
        self._taskQueue : queue.Queue[Callable[[], Any]] = queue.Queue()
        self._processTasksRequestedEventId : str = "fusion_main_thread_runner_" + str(uuid.uuid4())
        self._processTasksRequestedEvent = self._app.registerCustomEvent(self._processTasksRequestedEventId)
        self._processTasksRequestedEventHandler = self.ProcessTasksRequestedEventHandler(owner=self)
        self._processTasksRequestedEvent.add(self._processTasksRequestedEventHandler)

    def __del__(self):
        # clean up _processTasksRequestedEvent and the associated handler:
        try:
            if self._processTasksRequestedEventHandler and self._processTasksRequestedEvent:
                self._processTasksRequestedEvent.remove(self._processTasksRequestedEventHandler)

            if self._processTasksRequestedEvent:
                self._app.unregisterCustomEvent(self._processTasksRequestedEventId)
        except Exception:
            self._logger and self._logger.error("Error while unregistering event handler.",
                         exc_info=sys.exc_info())
        self._processTasksRequestedEventHandler = None
        self._processTasksRequestedEvent = None

    # def doTaskInMainFusionThread(self, task: Callable, wait: bool = False, suppressLogging: bool = False):
    def doTaskInMainFusionThread(self, task: Callable, wait: bool = False):
        # we ought to detect the case where this function is called and we are already in the main
        # fusion thread, because we may want to respond to the wait parameter differently in that case.
        
        if wait:
            _task = task
            waitLock = threading.Lock()
            waitLock.acquire()
            def task():
                result = _task()
                waitLock.release()
                # return result

        self._taskQueue.put(task)
        # result :bool = self._app.fireCustomEvent(self._processTasksRequestedEventId,additionalInfo=json.dumps({'suppressLogging':suppressLogging}))
        result :bool = self._app.fireCustomEvent(self._processTasksRequestedEventId)

        if wait:
            # wait for waitLock to be released, which will happen when task() is run in the main thread.
            waitLock.acquire()


    class ProcessTasksRequestedEventHandler(adsk.core.CustomEventHandler):
        def __init__(self, owner: 'FusionMainThreadRunner'):
            super().__init__()
            self._owner = owner

        def notify(self, args: adsk.core.CustomEventArgs):
            # we have to be rather cautious about writing to the log in here, because our log handlers
            # might themselves call doTaskInMainFusionThread().
            # desrializedAdditionalInfo = json.loads(args.additionalInfo)
            try:
                while True:
                    try:
                        self._owner._logger.debug("getting from queue...")
                        task = self._owner._taskQueue.get_nowait()
                        # self._owner._logger.debug("got from queue.")
                    except queue.Empty as e:
                        # self._owner._logger.debug("tried to get from an empty queue... breaking.")
                        break
                    self._owner._logger.debug("running a task that we have retrieved from the queue.")
                    result = task()
                    self._owner._taskQueue.task_done()

            except Exception:
                self._owner._logger and self._owner._logger.fatal("An error occurred while attempting to handle the processTasksRequested event", exc_info=sys.exc_info())
            finally:
                pass
