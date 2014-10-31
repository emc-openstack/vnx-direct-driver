# Copyright (c) 2014 EMC Corporation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import eventlet
from eventlet import greenthread
from eventlet import queue


class Status(object):
    NEW = 1
    OK = 0
    FAILURE = -1


class BatchWorkerBase(object):
    def __init__(self, task_executor, sleep_interval=10):
        self.queue = queue.Queue()
        self.executor = task_executor
        self.sleep_interval = sleep_interval
        self.thread = greenthread.spawn(self.run)

    def submit(self, order):
        self.queue.put(order)

    def get_pending_orders(self):
        orders = list()
        while True:
            try:
                order = self.queue.get_nowait()
                orders.append(order)
            except queue.Empty:
                break
        return orders

    def run(self):
        while True:
            try:
                orders = self.get_pending_orders()
                if len(orders) > 0:
                    self.executor(orders)
            except Exception as ex:
                print("execute exception %s" % ex)
            finally:
                eventlet.sleep(self.sleep_interval)
