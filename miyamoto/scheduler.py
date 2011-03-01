import socket

import gevent
import gevent.monkey
import gevent.server
import gevent.socket
import gevent.queue

from cluster import ClusterManager
from task import Task

gevent.monkey.patch_socket()


class ScheduleError(Exception): pass

class DistributedScheduler(object):
    def __init__(self, queue, leader, replica_factor=2, replica_offset=5, interface=None, 
        port=6001, cluster_port=6000):
        if interface is None:
            interface = socket.gethostbyname(socket.gethostname())
        self.interface = interface
        self.port = port
        
        self.cluster = ClusterManager(leader, callback=self._cluster_update, 
                            interface=interface, port=cluster_port)
        self.backend = gevent.server.StreamServer((interface, port), self._connection_handler)
        self.hosts = set()
        self.connections = {}
        
        self.queue = queue
        self.scheduled = {}
        self.scheduled_acks = {}
        self.schedules = 0
        
        self.replica_factor = replica_factor
        self.replica_offset = replica_offset
        
    def start(self):
        self.backend.start()
        self.cluster.start()
    
    def schedule(self, task):
        host_list = list(self.hosts)
        # This implements the round-robin N replication method for picking
        # which hosts to send the task. In short, every schedule moves along the
        # cluster ring by one, then picks N hosts, where N is level of replication
        host_ids = [(self.schedules + n) % len(host_list) for n in range(self.replica_factor)]
        hosts = [host_list[id] for id in host_ids]
        task.replica_hosts = hosts
        self.scheduled_acks[task.id] = gevent.queue.Queue()
        for host in hosts:
            self.connections[host].send('schedule:%s\n' % task.serialize())
            task.replica_offset += self.replica_offset
        try:
            return all([self.scheduled_acks[task.id].get(timeout=2) for h in hosts])
        except gevent.queue.Empty:
            raise ScheduleError("not all hosts acked")
        finally:
            self.schedules += 1 
            del self.scheduled_acks[task.id]
    
    def cancel(self, task):
        other_replica_hosts = set(task.replica_hosts) - set([self.interface])
        for host in other_replica_hosts:
            if host in self.connections:
                self.connections[host].send('cancel:%s\n' % task.id)
    
    def _cluster_update(self, hosts):
        add_hosts = hosts - self.hosts
        remove_hosts = self.hosts - hosts
        for host in remove_hosts:
            gevent.spawn(self.disconnect, host)
        for host in add_hosts:
            gevent.spawn(self.connect, host)
        self.hosts = hosts
    
    def connect(self, host):
        print "connecting to %s:%s" % (host, self.port)
        client = gevent.socket.create_connection((host, self.port), source_address=(self.interface, 0))
        self.connections[host] = client
        fileobj = client.makefile()
        while True:
            try:
                line = fileobj.readline().strip()
            except IOError:
                line = None
            if line:
                try:
                    self.scheduled_acks[line].put(True)
                except KeyError:
                    pass
            else:
                break
        print "disconnected from %s" % host
        del self.connections[host]
    
    def disconnect(self, host):
        if host in self.connections:
            print "disconnecting from %s" % host
            self.connections[host].shutdown(0)
            del self.connections[host]
    
    def _enqueue(self, task):
        self.queue.put(task)
        del self.scheduled[task.id]
    
    def _connection_handler(self, socket, address):
        fileobj = socket.makefile()
        while True:
            try:
                line = fileobj.readline().strip()
            except IOError:
                line = None
            if line:
                action, payload = line.split(':', 1)
                if action == 'schedule':
                    task = Task.unserialize(payload)
                    print "scheduled: %s" % task.id
                    self.scheduled[task.id] = gevent.spawn_later(task.time_until(), self._enqueue, task)
                    socket.send('%s\n' % task.id)
                elif action == 'cancel':
                    task_id = payload
                    print "canceled: %s" % task_id
                    self.scheduled[task_id].kill()
                    del self.scheduled[task_id]
                    #socket.send('%s\n' % task_id)
                elif action == 'delay':
                    print "internal delay"
                    task_id = payload
                    self.scheduled[task_id].start_later() # ummm
                    #socket.send('%s\n' % task_id)   
            else:
                break