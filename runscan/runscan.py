#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import sys
import argparse
import os
import time
import json
import requests
import warnings
import datetime
import pytz
from netaddr import IPNetwork, IPAddress, core
from requests.packages.urllib3 import exceptions as requestexp
from requests.auth import AuthBase

class ScanAPIAuth(AuthBase):
    def __init__(self, apikey):
        self._apikey = apikey

    def __call__(self, r):
        r.headers['SCANAPIKEY'] = self._apikey
        return r

class ScanAPIRequestor(object):
    def __init__(self, url, key, noverify=False):
        self._url = url
        self._key = key
        self._baseurl = url + '/api/v1'
        self._verify = True
        if noverify:
            self._verify = False
        self.body = None

    def _urlfrombase(self, ep):
        return self._baseurl + '/' + ep

    def request(self, ep, method, data=None, params=None, jsonresponse=True):
        if method == 'get':
            r = requests.get(self._urlfrombase(ep), auth=ScanAPIAuth(self._key), params=params,
                    verify=self._verify)
        elif method == 'delete':
            r = requests.delete(self._urlfrombase(ep), auth=ScanAPIAuth(self._key), params=params,
                    verify=self._verify)
        elif method == 'post':
            r = requests.post(self._urlfrombase(ep), auth=ScanAPIAuth(self._key),
                    data=data, verify=self._verify)
        else:
            raise ValueError('invalid request method')
        if r.status_code != requests.codes.ok:
            raise Exception('request failed with status code {}'.format(r.status_code))
        if jsonresponse:
            self.body = r.json()
        else:
            self.body = r.text

    def purge_scans(self, seconds):
        self.request('scan/purge', 'delete', params={'olderthan': int(seconds)})
        return self.body

    # XXX this is pretty inefficient right now as it pulls a result set down to validate
    # completion, we should have a better way to tell if the scan is done besides requesting
    # the results
    def request_scan_completed(self, scanid):
        return self.request_results(scanid)['completed']

    def request_results(self, scanid, mincvss=None, nooutput=False):
        noflag = None
        if nooutput:
            noflag = '1'
        self.request('scan/results', 'get', params={'scanid': scanid, 'mincvss': mincvss,
            'nooutput': noflag})
        return self.body

    def request_results_csv(self, scanid):
        self.request('scan/results/csv', 'get', params={'scanid': scanid},
                jsonresponse=False)
        return self.body

    def start_scan(self, targets, policy):
        payload = {'targets': targets, 'policy': policy}
        self.request('scan', 'post', data=payload)
        return self.body

    def request_policies(self):
        self.request('policies', 'get')
        return self.body

class ScanAPIMozDef(object):
    def __init__(self, resp, mozdef, mozdef_sourcename='scanapi'):
        self._sourcename = mozdef_sourcename
        self._url = mozdef
        self._events = [self._parse_result(x, resp['results']['zone']) for x in resp['results']['details']]
        self._use_stdout = False
        if self._url == 'stdout':
            self._use_stdout = True

    def post(self):
        if self._use_stdout:
            sys.stdout.write(json.dumps(self._events, indent=4) + '\n')
        else:
            for x in self._events:
                requests.post(self._url, data=json.dumps(x))

    def _parse_result(self, result, zone):
        event = {
                'description': 'scanapi runscan mozdef emitter',
                'sourcename': self._sourcename,
                'zone': zone,
                'version': 2,
                'utctimestamp':  pytz.timezone('UTC').localize(datetime.datetime.utcnow()).isoformat(),
                'asset': {
                    'hostname': result['hostname'],
                    'ipaddress': result['ipaddress'],
                    'os': result['os'],
                    },
                'vulnerabilities': result['vulnerabilities'],
                'scan_start': result['scan_start'],
                'scan_end': result['scan_end'],
                'credentialed_checks': result['credentialed_checks']
                }
        if 'owner' in result:
            event['asset']['owner'] = result['owner']
        if 'exempt_vulnerabilities' in result:
            event['exempt_vulnerabilities'] = result['exempt_vulnerabilities']
        return event

class ScanAPIServices(object):
    def __init__(self, response, sapi, sapikey=''):
        self._content = response
        self._sapiurl = sapi
        self._sapikey = sapikey

    def execute(self):
        self.execute_indicators()
        self.execute_ownership()
        return self._content

    def execute_indicators(self):
        # submit indicator to serviceapi for each host including if a credentialed
        # check was successful or not
        for x in self._content['results']['details']:
            # for the indicator value, find the highest level reported vulnerability in the
            # results for a given host; unknown if credentialed checks is false
            level = 1
            # seentitles tracks the titles of vulnerabilities we have already seen, so when
            # we are counting we don't count the same issue twice (e.g., more than one entry
            # may be present if a single vulnerability is represented my more than one
            # CVE
            seentitles = []
            details = {
                    'maximum': 0,
                    'high': 0,
                    'medium': 0,
                    'low': 0,
                    'coverage': False
                    }
            for v in x['vulnerabilities']:
                if v['risk'] == 'critical':
                    tv = 4
                    if v['name'] not in seentitles:
                        details['maximum'] += 1
                elif v['risk'] == 'high':
                    tv = 3
                    if v['name'] not in seentitles:
                        details['high'] += 1
                elif v['risk'] == 'medium':
                    tv = 2
                    if v['name'] not in seentitles:
                        details['medium'] += 1
                elif v['risk'] == 'low':
                    tv = 1
                    if v['name'] not in seentitles:
                        details['low'] += 1
                else:
                    tv = 0
                seentitles.append(v['name'])
                if tv > level:
                    level = tv
            if x['credentialed_checks']:
                details['coverage'] = True
                if level == 4:
                    lind = 'maximum'
                elif level == 3:
                    lind = 'high'
                elif level == 2:
                    lind = 'medium'
                else:
                    lind = 'low'
            else:
                lind = 'unknown'
            ind = {
                    'asset_type': 'hostname',
                    'asset_identifier': x['hostname'],
                    'zone': self._content['results']['zone'],
                    'description': 'scanapi vulnerability result',
                    'timestamp_utc': pytz.timezone('UTC').localize(datetime.datetime.utcnow()).isoformat(),
                    'event_source_name': 'scanapi',
                    'likelihood_indicator': lind,
                    'details': details
                    }
            headers = {'SERVICEAPIKEY': self._sapikey}
            r = requests.post(self._sapiurl + '/api/v1/indicator', data=json.dumps(ind), headers=headers)
            if r.status_code != 200:
                sys.stderr.write('warning: serviceapi indicator post failed with code {}\n'.format(r.status_code))

    def execute_ownership(self):
        hosts = set(x['hostname'] for x in self._content['results']['details'])
        respmap = {}
        for x in hosts:
            params = {'hostname': x}
            headers = {'SERVICEAPIKEY': self._sapikey}
            r = requests.get(self._sapiurl + '/api/v1/owner/hostname', params=params, headers=headers)
            if r.status_code == 200:
                respmap[x] = json.loads(r.text)
            else:
                respmap[x] = {
                        'operator': 'unset',
                        'team': 'unset',
                        'triagekey': 'unset-unset'
                        }
        for x in respmap:
            for y in range(len(self._content['results']['details'])):
                if self._content['results']['details'][y]['hostname'] != x:
                    continue
                self._content['results']['details'][y]['owner'] = {
                        'operator': respmap[x]['operator'],
                        'team': respmap[x]['team'],
                        'v2bkey': respmap[x]['triagekey']
                        }

requestor = None

def get_policies():
    resp = requestor.request_policies()
    for x in resp:
        sys.stdout.write('id={} name=\'{}\' description=\'{}\'\n'.format(x['id'],
            x['name'], x['description']))

def get_results(scanid, mozdef=None, mincvss=None, serviceapi=None, csv=False,
        nooutput=False):
    if serviceapi != None:
        sapikey = os.getenv('SERVICEAPIKEY')
        if sapikey == None:
            sys.stderr.write('Error: serviceapi integration requested but SERVICEAPIKEY not found in environment\n')
            sys.exit(1)
    if not requestor.request_scan_completed(scanid):
        sys.stdout.write('Scan incomplete\n')
        return
    if csv:
        sys.stdout.write(requestor.request_results_csv(scanid))
        return
    resp = requestor.request_results(scanid, mincvss=mincvss, nooutput=nooutput)
    if serviceapi != None:
        resp = ScanAPIServices(resp, serviceapi, sapikey=sapikey).execute()
    if mozdef == None:
        sys.stdout.write(json.dumps(resp, indent=4) + '\n')
    else:
        mozdef = ScanAPIMozDef(resp, mozdef)
        mozdef.post()

def purge_scans(seconds):
    sys.stdout.write(json.dumps(requestor.purge_scans(seconds), indent=4) + '\n')

def run_scan(targets, policy, follow=False, mozdef=None):
    # make sure the policy exists
    resp = requestor.request_policies()
    if not policy in [x['name'] for x in resp]:
        sys.stderr.write('Error: policy {} not found\n'.format(policy))
        sys.exit(1)
    # XXX should validate target list
    return requestor.start_scan(targets, policy)['scanid']

def load_subnet_filters(path):
    if path == None:
        return []
    fd = open(path, 'r')
    ret = fd.readlines()
    fd.close()
    return ret

def target_filter(t, filters):
    try:
        ip = IPAddress(t)
    except core.AddrFormatError: # not an ip
        return False
    for i in filters:
        if ip in IPNetwork(i):
            return True
    return False

def config_from_env():
    try:
        return {'apiurl': os.environ['SCANAPIURL'], 'apikey': os.environ['SCANAPIKEY']}
    except KeyError as e:
        sys.stderr.write('Error: environment variable {} not found\n'.format(str(e)))
        sys.exit(1)

def domain():
    global requestor
    warnings.simplefilter('ignore', requestexp.SubjectAltNameWarning)
    parser = argparse.ArgumentParser(epilog='The targets parameter can either contain' + \
            ' a comma separated list of targets, or a path to a file containing a target' + \
            ' list. If a file is used, it should contain one target per line.')
    parser.add_argument('--noverify', help='skip verification of certificates',
            action='store_true')
    parser.add_argument('--csv', help='fetch raw results in csv format instead of modified json',
            action='store_true')
    parser.add_argument('--filter-subnets', help='filter any ip in target list that matches a subnet' + \
            ' in subnetsfile', metavar='subnetsfile')
    parser.add_argument('--mozdef', help='emit results as vulnerability events to mozdef, ' + \
            'use \'stdout\' as url to just print json to stdout',
            metavar='mozdefurl')
    parser.add_argument('--mincvss', help='filter vulnerabilities below specified cvss score',
            metavar='cvss')
    parser.add_argument('--nooutput', help='don\'t include plugin output in results',
            action='store_true')
    parser.add_argument('--serviceapi', help='integrate with serviceapi for host ownership and indicators' +
            ', used when fetching results', metavar='sapiurl')
    parser.add_argument('-s', help='run scan on comma separated targets, can also be filename with targets',
            metavar='targets')
    parser.add_argument('-p', help='policy to use when running scan',
            metavar='policy')
    parser.add_argument('-D', help='purge scans older than argument, must be >= 300',
            metavar='seconds')
    parser.add_argument('-f', help='follow scan until complete and get results',
            action='store_true')
    parser.add_argument('-P', help='list policies', action='store_true')
    parser.add_argument('-r', help='fetch results', metavar='scan id')
    args = parser.parse_args()
    ecfg = config_from_env()
    requestor = ScanAPIRequestor(ecfg['apiurl'], ecfg['apikey'], noverify=args.noverify)
    if args.P:
        get_policies()
    elif args.r != None:
        get_results(args.r, mozdef=args.mozdef, mincvss=args.mincvss,
                serviceapi=args.serviceapi, csv=args.csv, nooutput=args.nooutput)
    elif args.D != None:
        purge_scans(args.D)
    elif args.s != None:
        if args.p == None:
            sys.stderr.write('Error: policy must be specified with -p\n')
            sys.exit(1)
        targets = None
        try:
            # if targets is a file, open it and build a target list
            with open(args.s, 'r') as fd:
                targets = ','.join([x.strip() for x in fd.readlines() if x[0] != '#'])
        except IOError:
            targets = args.s
        filters = load_subnet_filters(args.filter_subnets)
        targets = ','.join([x for x in targets.split(',') if not target_filter(x, filters)])
        scanid = run_scan(targets, args.p, follow=args.f, mozdef=args.mozdef)
        if args.f:
            while not requestor.request_scan_completed(scanid):
                time.sleep(15)
            get_results(scanid, mozdef=args.mozdef, mincvss=args.mincvss,
                    serviceapi=args.serviceapi, csv=args.csv, nooutput=args.nooutput)
        else:
            sys.stdout.write(scanid + '\n')
    else:
        sys.stdout.write('Must specify something to do\n\n')
        parser.print_help()
        sys.exit(1)
    sys.exit(0)

if __name__ == '__main__':
    domain()
