#!/usr/bin/python

import argparse
import os
import re
import time
from pprint import pprint
from sys import exit

import requests
from prometheus_client import start_http_server, Summary
from prometheus_client.core import GaugeMetricFamily, REGISTRY

DEBUG = int(os.environ.get('DEBUG', '0'))

COLLECTION_TIME = Summary('jenkins_collector_collect_seconds', 'Time spent to collect metrics from Jenkins')


class JenkinsCollector(object):
    # The build statuses we want to export about.
    statuses = ["lastBuild", "lastCompletedBuild", "lastFailedBuild",
                "lastStableBuild", "lastSuccessfulBuild", "lastUnstableBuild",
                "lastUnsuccessfulBuild"]

    def __init__(self, target, user, password, insecure):
        self._target = target.rstrip("/")
        self._user = user
        self._password = password
        self._insecure = insecure

    def collect(self):
        start = time.time()

        # Request data from Jenkins
        jobs = self._request_data()

        self._setup_empty_prometheus_metrics()

        for job in jobs:
            name = job['fullName']
            if DEBUG:
                print("Found Job: {}".format(name))
                pprint(job)
            self._get_metrics(name, job)

        for status in self.statuses:
            for metric in self._prometheus_metrics[status].values():
                yield metric

        for metric in self._job_runs_metrics.values():
            yield metric

        duration = time.time() - start
        COLLECTION_TIME.observe(duration)

    def _api_call(self, url, params):
        if self._user and self._password:
            response = requests.get(url, params=params, auth=(self._user, self._password), verify=(not self._insecure))
        else:
            response = requests.get(url, params=params, verify=(not self._insecure))
        if DEBUG:
            pprint(response.text)
        if response.status_code != requests.codes.ok:
            raise Exception("Call to url %s failed with status: %s" % (url, response.status_code))
        result = response.json()
        if DEBUG:
            pprint(result)

        return result

    def parse_job_runs(self, job):
        workflow_runs = {}
        if job['_class'] == 'org.jenkinsci.plugins.workflow.job.WorkflowJob' or job['_class'] == 'hudson.model.FreeStyleProject':
            builds = job.get('builds', [])
            if builds:
                successful_runs = []
                failed_runs = []
                for workflow_run in builds:
                    wf_data = self._api_call(workflow_run['url'] + 'api/json', {})
                    if wf_data['result'] == 'SUCCESS':
                        successful_runs.append(wf_data['number'])
                    if wf_data['result'] == 'FAILURE':
                        failed_runs.append(wf_data['number'])

                workflow_runs.update(
                    {'runs_successful_total': len(successful_runs),
                     'runs_failed_total': len(failed_runs)})
                job.update(workflow_runs)

    def parse_jobs(self, url, params):
        result = self._api_call(url, params)
        jobs = []
        for job in result['jobs']:
            if job['_class'] == 'com.cloudbees.hudson.plugins.folder.Folder' or \
                    job['_class'] == 'jenkins.branch.OrganizationFolder' or \
                    job['_class'] == 'org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject':
                jobs += self.parse_jobs(job['url'] + '/api/json', params)
            else:
                self.parse_job_runs(job)
                jobs.append(job)
        return jobs

    def _request_data(self):
        # Request exactly the information we need from Jenkins
        url = '{0}/api/json'.format(self._target)
        jobs = "[fullName,number,timestamp,duration,actions[queuingDurationMillis,totalDurationMillis," \
               "skipCount,failCount,totalCount,passCount]]"
        tree = 'jobs[fullName,url,builds[url],{0}]'.format(','.join([s + jobs for s in self.statuses]))
        params = {
            'tree': tree,
        }

        jobs_data = self.parse_jobs(url, params)
        return jobs_data

    def _setup_empty_prometheus_metrics(self):
        # The metrics we want to export.
        self._prometheus_metrics = {}
        for status in self.statuses:
            snake_case = re.sub('([A-Z])', '_\\1', status).lower()
            self._prometheus_metrics[status] = {
                'number':
                    GaugeMetricFamily('jenkins_job_{0}'.format(snake_case),
                                      'Jenkins build number for {0}'.format(status), labels=["jobname"]),
                'duration':
                    GaugeMetricFamily('jenkins_job_{0}_duration_seconds'.format(snake_case),
                                      'Jenkins build duration in seconds for {0}'.format(status), labels=["jobname"]),
                'timestamp':
                    GaugeMetricFamily('jenkins_job_{0}_timestamp_seconds'.format(snake_case),
                                      'Jenkins build timestamp in unixtime for {0}'.format(status), labels=["jobname"]),
                'queuingDurationMillis':
                    GaugeMetricFamily('jenkins_job_{0}_queuing_duration_seconds'.format(snake_case),
                                      'Jenkins build queuing duration in seconds for {0}'.format(status),
                                      labels=["jobname"]),
                'totalDurationMillis':
                    GaugeMetricFamily('jenkins_job_{0}_total_duration_seconds'.format(snake_case),
                                      'Jenkins build total duration in seconds for {0}'.format(status),
                                      labels=["jobname"]),
                'skipCount':
                    GaugeMetricFamily('jenkins_job_{0}_skip_count'.format(snake_case),
                                      'Jenkins build skip counts for {0}'.format(status), labels=["jobname"]),
                'failCount':
                    GaugeMetricFamily('jenkins_job_{0}_fail_count'.format(snake_case),
                                      'Jenkins build fail counts for {0}'.format(status), labels=["jobname"]),
                'totalCount':
                    GaugeMetricFamily('jenkins_job_{0}_total_count'.format(snake_case),
                                      'Jenkins build total counts for {0}'.format(status), labels=["jobname"]),
                'passCount':
                    GaugeMetricFamily('jenkins_job_{0}_pass_count'.format(snake_case),
                                      'Jenkins build pass counts for {0}'.format(status), labels=["jobname"]),
            }

        self._job_runs_metrics = {}
        self._job_runs_metrics = {
            'runs_successful_total': GaugeMetricFamily('jenkins_runs_successful_total', 'Jenkins total job successful runs',
                                                 labels=["jobname"]),
            'runs_failed_total': GaugeMetricFamily('jenkins_runs_failed_total', 'Jenkins total job failed runs',
                                                 labels=["jobname"]),
        }

    def _get_metrics(self, name, job):
        for status in self.statuses:
            if status in job.keys():
                status_data = job[status] or {}
                self._add_data_to_prometheus_structure(status, status_data, job, name)

    def _add_data_to_prometheus_structure(self, status, status_data, job, name):
        # If there's a null result, we want to pass.
        if status_data.get('duration', 0):
            self._prometheus_metrics[status]['duration'].add_metric([name], status_data.get('duration') / 1000.0)
        if status_data.get('timestamp', 0):
            self._prometheus_metrics[status]['timestamp'].add_metric([name], status_data.get('timestamp') / 1000.0)
        if status_data.get('number', 0):
            self._prometheus_metrics[status]['number'].add_metric([name], status_data.get('number'))
        actions_metrics = status_data.get('actions', [{}])
        for metric in actions_metrics:
            if metric.get('queuingDurationMillis', False):
                self._prometheus_metrics[status]['queuingDurationMillis'].add_metric([name], metric.get(
                    'queuingDurationMillis') / 1000.0)
            if metric.get('totalDurationMillis', False):
                self._prometheus_metrics[status]['totalDurationMillis'].add_metric([name], metric.get(
                    'totalDurationMillis') / 1000.0)
            if metric.get('skipCount', False):
                self._prometheus_metrics[status]['skipCount'].add_metric([name], metric.get('skipCount'))
            if metric.get('failCount', False):
                self._prometheus_metrics[status]['failCount'].add_metric([name], metric.get('failCount'))
            if metric.get('totalCount', False):
                self._prometheus_metrics[status]['totalCount'].add_metric([name], metric.get('totalCount'))
                # Calculate passCount by subtracting fails and skips from totalCount
                passcount = metric.get('totalCount') - metric.get('failCount') - metric.get('skipCount')
                self._prometheus_metrics[status]['passCount'].add_metric([name], passcount)

        if job.get('runs_successful_total', 0):
            self._job_runs_metrics['runs_successful_total'].add_metric([name], job.get('runs_successful_total'))
        if job.get('runs_failed_total', 0):
            self._job_runs_metrics['runs_failed_total'].add_metric([name], job.get('runs_failed_total'))


def parse_args():
    parser = argparse.ArgumentParser(
        description='jenkins exporter args jenkins address and port'
    )
    parser.add_argument(
        '-j', '--jenkins',
        metavar='jenkins',
        required=False,
        help='server url from the jenkins api',
        default=os.environ.get('JENKINS_SERVER', 'http://jenkins:8080')
    )
    parser.add_argument(
        '--user',
        metavar='user',
        required=False,
        help='jenkins api user',
        default=os.environ.get('JENKINS_USER')
    )
    parser.add_argument(
        '--password',
        metavar='password',
        required=False,
        help='jenkins api password',
        default=os.environ.get('JENKINS_PASSWORD')
    )
    parser.add_argument(
        '-p', '--port',
        metavar='port',
        required=False,
        type=int,
        help='Listen to this port',
        default=int(os.environ.get('VIRTUAL_PORT', '9118'))
    )
    parser.add_argument(
        '-k', '--insecure',
        dest='insecure',
        required=False,
        action='store_true',
        help='Allow connection to insecure Jenkins API',
        default=False
    )
    return parser.parse_args()


def main():
    try:
        args = parse_args()
        port = int(args.port)
        REGISTRY.register(JenkinsCollector(args.jenkins, args.user, args.password, args.insecure))
        start_http_server(port)
        print("Polling {}. Serving at port: {}".format(args.jenkins, port))
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(" Interrupted")
        exit(0)


if __name__ == "__main__":
    main()
