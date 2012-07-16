import logging
import json
import datetime
import functools
from collections import defaultdict
from django import http
from django.shortcuts import render, redirect
from django.conf import settings
from django.core.urlresolvers import reverse

from session_csrf import anonymous_csrf

from . import models
from . import forms
from . import utils


def plot_graph(start_date, end_date, adubyday, currentversions):
    throttled = {}
    for v in currentversions:
        if v['product'] == adubyday['product']:
            throttled[v['version']] = float(v['throttle'])

    graph_data = {
        'startDate': adubyday['start_date'],
        'endDate': end_date.strftime('%Y-%m-%d'),
        'count': len(adubyday['versions']),
    }

    for i, version in enumerate(adubyday['versions'], start=1):
        graph_data['item%s' % i] = version['version']
        graph_data['ratio%s' % i] = []
        points = defaultdict(int)
        for s in version['statistics']:
            time_ = utils.unixtime(s['date'], millis=True)
            if time_ in points:
                (crashes, users) = points[time_]
            else:
                crashes = users = 0
            users += s['users']
            crashes += s['crashes']
            points[time_] = (crashes, users)

        for day in utils.daterange(start_date, end_date):
            time_ = utils.unixtime(day, millis=True)

            if time_ in points:
                (crashes, users) = points[time_]
                t = throttled[version['version']]
                if t != 100:
                    t *= 100
                if users == 0:
                    logging.warning('no ADU data for %s' % day)
                    continue
                ratio = float(crashes) / float(users) * t
            else:
                ratio = None

            graph_data['ratio%s' % i].append([int(time_), ratio])

    return graph_data


def set_base_data(view):

    def _basedata(product=None, versions=None):
        """
        from @product and @versions transfer to
        a dict. If there's any left-over, raise a 404 error
        """
        data = {}
        api = models.CurrentVersions()
        data['currentversions'] = api.get()
        if versions is None:
            versions = []
        else:
            versions = versions.split(';')

        for release in data['currentversions']:
            if product == release['product']:
                data['product'] = product
                if release['version'] in versions:
                    versions.remove(release['version'])
                    if 'versions' not in data:
                        data['versions'] = []
                    data['versions'].append(release['version'])

        if product is None:
            # thus a view that doesn't have a product in the URL
            # e.g. like /query
            if not data.get('product'):
                data['product'] = settings.DEFAULT_PRODUCT
        elif product != data.get('product'):
            raise http.Http404("Not a recognized product")

        if product and versions:
            raise http.Http404("Not a recognized version for that product")

        return data

    @functools.wraps(view)
    def inner(request, *args, **kwargs):
        product = kwargs.get('product', None)
        versions = kwargs.get('versions', None)
        for key, value in _basedata(product, versions).items():
            setattr(request, key, value)
        return view(request, *args, **kwargs)

    return inner


@set_base_data
def products(request, product, versions=None):
    data = {}

    # FIXME hardcoded default, find a better place for this to live
    os_names = ['Windows', 'Mac', 'Linux']

    duration = request.GET.get('duration')

    if duration is None or duration not in ['3', '7', '14']:
        duration = 7
    else:
        duration = int(duration)

    data['duration'] = duration

    if versions is None:
        versions = []
        for release in request.currentversions:
            if release['product'] == request.product and release['featured']:
                versions.append(release['version'])
    else:
        versions = versions.split(';')

    if len(versions) == 1:
        data['version'] = versions[0]

    end_date = datetime.datetime.utcnow()
    start_date = end_date - datetime.timedelta(days=duration + 1)

    mware = models.ADUByDay()
    adubyday = mware.get(product, versions, os_names,
                         start_date, end_date)
    data['graph_data'] = json.dumps(
        plot_graph(start_date, end_date, adubyday, request.currentversions)
    )
    data['report'] = 'products'
    return render(request, 'crashstats/products.html', data)


@set_base_data
@anonymous_csrf
def topcrasher(request, product=None, versions=None, days=None,
               crash_type=None, os_name=None):
    data = {}

    if not versions:
        # :(
        # simulate what the nav.js does which is to take the latest version
        # for this product.
        for release in request.currentversions:
            if release['product'] == product and release['featured']:
                url = reverse('crashstats.topcrasher',
                              kwargs=dict(product=product,
                                          versions=release['version']))
                return redirect(url)
    else:
        versions = versions.split(';')

    if len(versions) == 1:
        data['version'] = versions[0]

    if days not in ['1', '3', '7', '14', '28']:
        days = 7
    days = int(days)
    data['days'] = days

    end_date = datetime.datetime.utcnow()

    if crash_type not in ['all', 'browser', 'plugin', 'content']:
        crash_type = 'browser'

    data['crash_type'] = crash_type

    if os_name not in ['Windows', 'Linux', 'Mac OS X']:
        os_name = None

    data['os_name'] = os_name

    api = models.TCBS()
    tcbs = api.get(product, data['version'], crash_type, end_date,
                    duration=(days * 24), limit='300')
    signatures = [c['signature'] for c in tcbs['crashes']]

    bugs = defaultdict(list)
    api = models.Bugs()
    for b in api.get(signatures)['bug_associations']:
        bugs[b['signature']].append(b['bug_id'])

    for crash in tcbs['crashes']:
        sig = crash['signature']
        if sig in bugs:
            if 'bugs' in crash:
                crash['bugs'].extend(bugs[sig])
            else:
                crash['bugs'] = bugs[sig]

    data['tcbs'] = tcbs
    data['report'] = 'topcrasher'

    return render(request, 'crashstats/topcrasher.html', data)


@set_base_data
def daily(request):
    data = {}

    form_selection = request.GET.get('form_selection', 'by_version')
    product = request.GET.get('p', settings.DEFAULT_PRODUCT)
    os_names = request.GET.getlist('os[]', ['Windows', 'Mac', 'Linux'])
    hang_type = request.GET.get('hang_type', 'any')
    report_type = request.GET.getlist('report_type[]')
    throttle = request.GET.getlist('throttle[]')

    versions = request.GET.getlist('v[]')
    # delete empty strings from the array
    versions = [version for version in versions if not version == '']
    if versions == []:
        for release in request.currentversions:
            if release['product'] == product and release['featured']:
                versions.append(release['version'])

    end_date = request.GET.get('date_end', datetime.datetime.utcnow())
    if type(end_date) is unicode:
        end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d')
    start_date = request.GET.get('date_start', 
                                 end_date - datetime.timedelta(days=8))
    if type(start_date) is unicode:
        start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')

    api = models.ADUByDay()
    if(form_selection == 'by_report_type'):
        adubyday = api.get(product, versions, os_names, start_date, end_date,
                       report_type=report_type)
    else:
        adubyday = api.get(product, versions, os_names, start_date, end_date,
                       report_type=[hang_type])

    data['graph_data'] = json.dumps(
        plot_graph(start_date, end_date, adubyday, request.currentversions)
    )
    data['report'] = 'daily'
    data['form_selection'] = form_selection 
    data['product'] = product
    data['versions'] = versions
    data['start_date'] = start_date.strftime('%Y-%m-%d')
    data['end_date'] = end_date.strftime('%Y-%m-%d')
    data['throttle'] = throttle
    data['os_names'] = os_names
    data['hang_type'] = hang_type
    data['report_type'] = report_type

    return render(request, 'crashstats/daily.html', data)


@set_base_data
def builds(request, product=None, versions=None):
    data = {}

    # the model DailyBuilds only takes 1 version if possible.
    # however, the way our set_base_data decorator works we have to call it
    # versions (plural) even though we here don't support that
    if versions is not None:
        assert isinstance(versions, basestring)

        request.version = versions  # so it's available in the template

    data['report'] = 'builds'
    api = models.DailyBuilds()
    middleware_results = api.get(product, version=versions)
    builds = defaultdict(list)
    for build in middleware_results:
        if build['build_type'] != 'Nightly':
            continue
        key = '%s%s' % (build['date'], build['version'])
        build['date'] = datetime.datetime.strptime(
          build['date'],
          '%Y-%m-%d'
        )
        builds[key].append(build)

    # lastly convert it to a list of tuples
    all_builds = []
    # sort by the key but then ignore it...
    for __, individual_builds in sorted(builds.items(), reverse=True):
        # ...by using the first item to get the date and version
        first_build = individual_builds[0]
        all_builds.append((
          first_build['date'],
          first_build['version'],
          individual_builds
        ))

    data['all_builds'] = all_builds
    return render(request, 'crashstats/builds.html', data)


@set_base_data
def hangreport(request, product=None, versions=None, listsize=5):
    data = {}

    try:
        page = int(request.GET.get('page', 1))
    except ValueError:
        return http.HttpResponseBadRequest('Invalid page')

    duration = request.GET.get('duration', 7)
    if duration not in (3, 7, 14, 28):
        return http.HttpResponseBadRequest('Invalid duration')
    data['duration'] = int(duration)

    end_date = datetime.datetime.utcnow().strftime('%Y-%m-%d')

    hangreport = models.HangReport()
    assert versions
    data['hangreport'] = hangreport.get(product, versions, end_date, duration,
                                        listsize, page)
    data['report'] = 'hangreport'
    if page > data['hangreport']['totalPages']:
        # naughty parameter, go to the last page
        if isinstance(versions, (list, tuple)):
            versions = ';'.join(versions)
        url = reverse('crashstats.hangreport',
                      args=[product, versions])
        url += ('?duration=%s&page=%s'
                % (duration, data['hangreport']['totalPages']))
        return redirect(url)

    data['current_page'] = page
    return render(request, 'crashstats/hangreport.html', data)


@set_base_data
def topchangers(request, product=None, versions=None, duration=7):
    data = {}

    if request.GET.get('duration'):
        # the old URL
        url = reverse('crashstats.topchangers',
                      kwargs=dict(product=product,
                                  versions=versions,
                                  duration=duration))
        return redirect(url)

    duration = int(duration)
    if duration not in (3, 7, 14, 28):
        return http.HttpResponseBadRequest('Invalid duration')
    data['duration'] = duration

    all_versions = []
    if versions is None:
        for release in request.currentversions:
            if release['product'] == request.product and release['featured']:
                all_versions.append(release['version'])
    else:
        # xxx: why is it called "versions" when it's a single value?
        all_versions.append(versions)

    data['versions'] = all_versions

    end_date = datetime.datetime.utcnow()

    # FIXME hardcoded crash_type
    crash_type = 'browser'

    changers = defaultdict(list)
    api = models.TCBS()
    for v in all_versions:
        tcbs = api.get(product, v, crash_type, end_date,
                       duration=duration * 24, limit='300')

        for crash in tcbs['crashes']:
            if crash['changeInRank'] != 'new':
                change = int(crash['changeInRank'])
                if change <= 0:
                    continue
                changers[change].append(crash)

    data['topchangers'] = changers

    data['report'] = 'topchangers'
    return render(request, 'crashstats/topchangers.html', data)


@set_base_data
def report_index(request, crash_id=None):
    data = {}

    api = models.ProcessedCrash()
    data['report'] = api.get(crash_id)

    process_type = 'unknown'
    if data['report']['process_type'] is None:
        process_type = 'browser'
    elif data['report']['process_type'] == 'plugin':
        process_type = 'plugin'
    elif data['report']['process_type'] == 'content':
        process_type = 'content'
    data['process_type'] = process_type

    data['product'] = data['report']['product']
    data['version'] = data['report']['version']

    modules = []
    threads = {}
    for line in data['report']['dump'].split('\n'):
        entry = line.split('|')
        if entry[0] == 'Module':
            modules.append({
                'filename': entry[1],
                'version': entry[2],
                'debug_filename': entry[3],
                'debug_identifier': entry[4]
            })
        elif entry[0].isdigit():
            thread_number = int(entry[0])
            frame = {
                'number': int(entry[1]),
                'module': entry[2],
                'signature': entry[3],
                'source': entry[4],
                'FIXME': entry[5],
                'address': entry[6]
            }
            # crashing thread is listed first
            if threads == {}:
                data['crashing_thread'] = thread_number

            if thread_number in threads:
                threads[thread_number].append(frame)
            else:
                threads[thread_number] = [frame]

    data['modules'] = modules
    data['threads'] = threads

    bugs_api = models.Bugs()
    data['bug_associations'] = bugs_api.get(
      [data['report']['signature']]
    )['bug_associations']

    end_date = datetime.datetime.utcnow()
    start_date = end_date - datetime.timedelta(days=14)

    comments_api = models.CommentsBySignature()
    data['comments'] = comments_api.get(data['report']['signature'],
                                        start_date, end_date)

    raw_api = models.RawCrash()
    data['raw'] = raw_api.get(crash_id)

    if 'HangID' in data['raw']:
        data['hang_id'] = data['raw']['HangID']

        crash_pair_api = models.CrashPairsByCrashId()
        data['crash_pairs'] = crash_pair_api.get(
          data['report']['uuid'],
          data['hang_id']
        )

    return render(request, 'crashstats/report_index.html', data)


@set_base_data
def report_list(request):
    data = {}

    signature = request.GET.get('signature')
    product_version = request.GET.get('version')
    end_date = datetime.datetime.strptime(request.GET.get('date'), '%Y-%m-%d')
    duration = int(request.GET.get('range_value'))

    start_date = end_date - datetime.timedelta(days=duration)
    data['start_date'] = start_date.strftime('%Y-%m-%d')

    result_number = 250

    api = models.ReportList()
    data['report_list'] = api.get(signature, product_version,
                                  start_date, result_number)

    return render(request, 'crashstats/report_list.html', data)


@set_base_data
def query(request):
    data = {}

    api = models.Search()
    # XXX why on earth are these numbers hard-coded?
    data['query'] = api.get(product='Firefox',
        versions='13.0a1;14.0a2;13.0b2;12.0', os_names='Windows;Mac;Linux',
        start_date='2012-05-03', end_date='2012-05-10', limit='100')

    return render(request, 'crashstats/query.html', data)


@utils.json_view
def buginfo(request, signatures=None):
    form = forms.BugInfoForm(request.GET)
    if not form.is_valid():
        return http.HttpResponseBadRequest(str(form.errors))

    bugs = form.cleaned_data['bug_ids']
    fields = form.cleaned_data['include_fields']

    bzapi = models.BugzillaBugInfo()
    return bzapi.get(bugs, fields)


@set_base_data
@utils.json_view
def plot_signature(request, product, versions, start_date, end_date,
                   signature):
    date_format = '%Y-%m-%d'
    try:
        start_date = datetime.datetime.strptime(start_date, date_format)
        end_date = datetime.datetime.strptime(end_date, date_format)
    except ValueError, msg:
        return http.HttpResponseBadRequest(str(msg))

    diff = end_date - start_date
    duration = diff.days * 24.0 + diff.seconds / 3600.0

    api = models.SignatureTrend()
    sigtrend = api.get(product, versions, signature, end_date, duration)

    graph_data = {
        'startDate': sigtrend['start_date'],
        'signature': sigtrend['signature'],
        'endDate': sigtrend['end_date'],
        'counts': [],
        'percents': [],
    }

    for s in sigtrend['signatureHistory']:
        t = utils.unixtime(s['date'], millis=True)
        graph_data['counts'].append([t, s['count']])
        graph_data['percents'].append([t, (s['percentOfTotal'] * 100)])

    return graph_data


@utils.json_view
def signature_summary(request):
    #try:
    #    range_value = int(request.GET.get('range_value'))
    #except ValueError, msg:
    #    return http.HttpResponseBadRequest(str(msg))

    #range_unit = request.GET.get('range_unit')
    signature = request.GET.get('signature')
    #product_version = request.GET.get('version')
    try:
        start_date = datetime.datetime.strptime(request.GET.get('date'),
                                                '%Y-%m-%d')
    except ValueError, msg:
        return http.HttpResponseBadRequest(str(msg))
    end_date = datetime.datetime.utcnow()

    report_types = {
        'architecture': 'architectures',
        'flash_version': 'flashVersions',
        'os': 'percentageByOs',
        'process_type': 'processTypes',
        'products': 'productVersions',
        'uptime': 'uptimeRange'
    }

    api = models.SignatureSummary()

    result = {}
    signature_summary = {}
    for r in report_types:
        name = report_types[r]
        result[name] = api.get(r, signature, start_date, end_date)
        signature_summary[name] = []

    # FIXME fix JS so it takes above format..
    for r in result['architectures']:
        signature_summary['architectures'].append({
            'architecture': r['category'],
            'percentage': (float(r['percentage']) * 100),
            'numberOfCrashes': r['report_count']})
    for r in result['percentageByOs']:
        signature_summary['percentageByOs'].append({
            'os': r['category'],
            'percentage': (float(r['percentage']) * 100),
            'numberOfCrashes': r['report_count']})
    for r in result['productVersions']:
        signature_summary['productVersions'].append({
            'product': r['product_name'],
            'version': r['version_string'],
            'percentage': r['percentage'],
            'numberOfCrashes': r['report_count']})
    for r in result['uptimeRange']:
        signature_summary['uptimeRange'].append({
            'range': r['category'],
            'percentage': (float(r['percentage']) * 100),
            'numberOfCrashes': r['report_count']})
    for r in result['processTypes']:
        signature_summary['processTypes'].append({
            'processType': r['category'],
            'percentage': (float(r['percentage']) * 100),
            'numberOfCrashes': r['report_count']})
    for r in result['flashVersions']:
        signature_summary['flashVersions'].append({
            'flashVersion': r['category'],
            'percentage': (float(r['percentage']) * 100),
            'numberOfCrashes': r['report_count']})

    return signature_summary
