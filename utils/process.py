import time
import logging
from . import communication
from . import database
from . import scheduler
from . import policies
from datetime import datetime
from multiprocessing import Queue
from configparser import ConfigParser


# ---------- Processos de Monitoramento ----------
# Metodo de monitoramento do host e envio para o servidor global


def host_monitor(host_queue: Queue):

	while True:
		logging.debug('Starting Host Monitor')

		try:
			host = host_queue.get()
			host.update()
			host.update_containers()

			container_list = host.container_active_list + host.container_inactive_list

			for container in container_list:
				database.publish_local_container_history(container)

			logging.debug('Send Monitoring Data to Manager')
			logging.debug('Sended Host Data: %s', vars(host))
			communication.send_monitor_data(host)

			# Remove Finished Containers
			host.remove_finished_containers()

			host_queue.put(host)
			logging.debug('Host Monitor Sleeping')

		except Exception as err:
			logging.error('Monitor error: %s', err)

		time.sleep(1)


# Metodo global de monitoramento de todos os hosts e recebimento de informações


def global_monitor(host_list, request_list):

	while True:
		data, host = communication.receive_monitor_data()

		if host:
			logging.debug('Received Data from Host %s', host.hostname)
			logging.debug('Received Host Data: %s', vars(host))

			# Atualização dos containers e hosts no banco de dados
			database.publish_host(host.hostname, data)
			container_list = host.container_active_list + host.container_inactive_list

			for container in container_list:
				database.publish_container_history(container)
				database.update_container_status(container)

			# Manutenção da listas de hosts disponíveis
			if host in host_list:
				index = host_list.index(host)
				host_list[index] = host
			else:
				host_list.append(host)

			# Atualização dos containers nos respectivos requests
			for request in request_list:
				index = request_list.index(request)
				request.check_container_status(container_list)
				modified = request.change_status()
				request_list[index] = request
				logging.debug('Monitoring Request: %s, Status: %s, Containers: %s', request_list[index].reqid, request_list[index].status, request_list[index].listcontainers)

				if modified:
					database.update_request_status(request.reqid, request.status)

				if request.status == 'FINISHED':
					logging.info('Request Finished: %s', request.reqid)
					request_list.remove(request)

			print('Host List: ', host_list)
			print('Request List: ', request_list)


# ---------- Processo de Escalonamento Global ------------


def global_scheduler(host_list, request_list):
	new_req_list = []

	while True:
		logging.debug('Starting Global Scheduler')
		new_req_list += database.get_new_requests()

		for req in new_req_list:
			if req.status == 'NEW':
				req.status = 'QUEUED'
				database.update_request_status(req.reqid, req.status)

		scheduler.one_host_global_scheduler2(host_list, request_list, new_req_list)
		logging.debug('Global Scheduler Sleeping')
		time.sleep(10)


# ---------- Processo de Recebimento de Requisições -----------
# Metodo de recebimento, no host, de requisições vindas do escalonador global


def request_receiver(entry_queue: Queue):
	while True:
		container = communication.receive_container_request()
		config = ConfigParser()
		config.read('./config/local-config.txt')

		if container:
			logging.info('Received New Container %s and Added to Entry List', container.name)
			logging.debug('New Container: %s', vars(container))

			if config['Container']['type'] == 'LXC':
				container.createContainer()

			container.inactive_time = datetime.now()

		entry_list = entry_queue.get()
		entry_list.append(container)
		entry_queue.put(entry_list)


# ----------- Processo de Gerência dos Containers ----------
# Metodo de gerência dos containers em um host


def container_manager(host_queue: Queue, entry_queue: Queue):
	cooldown_list = []

	while True:
		print('=========================')
		logging.debug('Starting Container Manager')
		host = host_queue.get()
		host.update()
		host.update_containers()

		print('Active List:', host.container_active_list)
		print('Inactive List:', host.container_inactive_list)
		print('Core Allocation List:', host.core_allocation)

		for cooldown in cooldown_list:
			if not host.is_active_container(cooldown['name']):
				cooldown_list.remove(cooldown)

		print('Cooldown List:', cooldown_list)

		# Add Created Containers
		entry_list = entry_queue.get()
		print('Entry List:', entry_list)

		for container in entry_list:
			host.container_inactive_list.append(container)
			entry_list.remove(container)

		entry_queue.put(entry_list)

		free_mem = host.get_available_memory()

		print('Free Memory Before Policy: ' + str(free_mem // 2 ** 20) + 'MB')
		# free_mem = policies.memory_shaping_policy(host, free_mem)
		# free_mem = policies.ED_policy(host, free_mem, cooldown_list)
		print('Free Memory After Policy: ' + str(free_mem // 2 ** 20) + 'MB')

		if (free_mem > 0) and host.has_free_cores() and host.has_inactive_containers():
			policies.start_container_policy(host, free_mem)

		host_queue.put(host)
		logging.debug('Container Manager Sleeping')
		time.sleep(10)