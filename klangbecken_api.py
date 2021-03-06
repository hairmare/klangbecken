#!/usr/bin/python3
from __future__ import print_function, unicode_literals, division

import json
import os
import subprocess
import sys
from collections import Counter
from io import open
from os.path import join as pjoin
from xml.etree import ElementTree

import mutagen
from mutagen.easyid3 import EasyID3
from werkzeug.contrib.securecookie import SecureCookie
from werkzeug.exceptions import (HTTPException, UnprocessableEntity, NotFound,
                                 Unauthorized)
from werkzeug.routing import Map, Rule
from werkzeug.utils import secure_filename, cached_property
from werkzeug.wrappers import BaseRequest, Response
from werkzeug.wsgi import wrap_file


PLAYLISTS = ['music', 'jingles']


class JSONSecureCookie(SecureCookie):
    serialization_method = json


class Request(BaseRequest):

    @cached_property
    def client_session(self):
        secret_key = os.environ['KLANGBECKEN_API_SECRET']
        return SecureCookie.load_cookie(self, secret_key=secret_key)


class KlangbeckenAPI:

    def __init__(self, stand_alone=False):
        self.data_dir = os.environ.get('KLANGBECKEN_DATA',
                                       '/var/lib/klangbecken')
        self.secret = os.environ['KLANGBECKEN_API_SECRET']
        self.url_map = Map()

        # register the TXXX key so that we can access it later as
        # mutagenfile['rg_track_gain']
        EasyID3.RegisterTXXXKey(key='track_gain',
                                desc='REPLAYGAIN_TRACK_GAIN')
        EasyID3.RegisterTXXXKey(key='cue_in',
                                desc='CUE_IN')
        EasyID3.RegisterTXXXKey(key='cue_out',
                                desc='CUE_OUT')

        root_url = '/<any(' + ', '.join(PLAYLISTS) + '):category>/'

        mappings = [
            ('/login/', ('GET', 'POST'), 'login'),
            ('/logout/', ('POST',), 'logout'),
            (root_url, ('GET',), 'list'),
            (root_url + '<filename>', ('GET',), 'get'),
            (root_url, ('POST',), 'upload'),
            (root_url + '<filename>', ('PUT',), 'update'),
            (root_url + '<filename>', ('DELETE',), 'delete'),
        ]

        if stand_alone:
            # Serve html and prefix calls to api
            mappings = [('/api' + path, methods, endpoint)
                        for path, methods, endpoint in mappings]
            mappings.append(('/', ('GET',), 'static'))
            mappings.append(('/<path:path>', ('GET',), 'static'))
            cur_dir = os.path.dirname(os.path.realpath(__file__))
            dist_dir = open(pjoin(cur_dir, '.dist_dir')).read().strip()
            self.static_dir = pjoin(cur_dir, dist_dir)

        for path, methods, endpoint in mappings:
            self.url_map.add(Rule(path, methods=methods, endpoint=endpoint))

    def _full_path(self, path):
        return pjoin(self.data_dir, path)

    def _replaygain_analysis(self, mutagenfile):
        bs1770gain_cmd = [
            "/usr/bin/bs1770gain", "--ebu", "--xml", mutagenfile.filename
        ]
        output = subprocess.check_output(bs1770gain_cmd)
        bs1770gain = ElementTree.fromstring(output)
        # lu is in bs1770gain > album > track > integrated as an attribute
        track_gain = bs1770gain.find('./album/track/integrated').attrib['lu']
        mutagenfile['track_gain'] = track_gain + ' dB'

    def _silan_analysis(self, mutagenfile):
        silan_cmd = [
            '/usr/bin/silan', '--format', 'json', mutagenfile.filename
        ]
        output = subprocess.check_output(silan_cmd)
        cue_points = json.loads(output)['sound'][0]
        mutagenfile['cue_in'] = str(cue_points[0])
        mutagenfile['cue_out'] = str(cue_points[1])

    def __call__(self, environ, start_response):
        request = Request(environ)
        adapter = self.url_map.bind_to_environ(request.environ)

        session = request.client_session
        try:
            endpoint, values = adapter.match()
            if endpoint not in ['login', 'static'] and (session.new or
                                                        'user' not in session):
                raise Unauthorized()
            response = getattr(self, 'on_' + endpoint)(request, **values)
        except HTTPException as e:
            response = e
        return response(environ, start_response)

    def on_login(self, request):
        if request.remote_user is None:
            raise Unauthorized()

        response = Response(json.dumps({'status': 'OK'}), mimetype='text/json')
        session = request.client_session
        session['user'] = request.environ['REMOTE_USER']
        session.save_cookie(response)
        return response

    def on_logout(self, request):
        response = Response(json.dumps({'status': 'OK'}), mimetype='text/json')
        session = request.client_session
        del session['user']
        session.save_cookie(response)
        return response

    def on_list(self, request, category):
        cat_dir = self._full_path(category)
        filenames = os.listdir(cat_dir)
        tuples = [(filename, os.path.join(category, filename))
                  for filename in filenames]
        tuples = [(filename, path,
                   mutagen.File(self._full_path(path), easy=True))
                  for (filename, path) in tuples
                  if os.path.isfile(self._full_path(path))
                  and path.endswith('.mp3')]
        counter = Counter(path.strip() for path in
                          open(self._full_path(category + ".m3u")).readlines())
        # FIXME: cue-points and replaygain
        dicts = [
            {
                'filename': filename,
                'path': path,
                'artist': mutagenfile.get('artist', [''])[0],
                'title': mutagenfile.get('title', [''])[0],
                'album': mutagenfile.get('album', [''])[0],
                'length': float(mutagenfile.info.length),
                'mtime': os.stat(self._full_path(path)).st_mtime,
                'repeate': counter[path],
            } for (filename, path, mutagenfile) in tuples
        ]

        data = sorted(dicts, key=lambda v: v['mtime'], reverse=True)
        return Response(json.dumps(data, indent=2, sort_keys=True,
                                   ensure_ascii=True), mimetype='text/json')

    def on_get(self, request, category, filename):
        path = pjoin(category, secure_filename(filename))
        full_path = self._full_path(path)
        if not os.path.exists(full_path):
            raise NotFound()
        return Response(wrap_file(request.environ, open(full_path, 'rb')),
                        mimetype='audio/mpeg')

    def on_upload(self, request, category):
        file = request.files['files']

        if not file:
            raise UnprocessableEntity()

        filename = secure_filename(file.filename)
        # filename = gen_file_name(filename) # FIXME: check duplicate filenames
        # mimetype = file.content_type

        if not file.filename.endswith('.mp3'):
            raise UnprocessableEntity('Filetype not allowed ')

        # save file to disk
        file_path = pjoin(category, filename)
        file.save(self._full_path(file_path))
        with open(self._full_path(category + '.m3u'), 'a') as f:
            print(file_path, file=f)

        # FIXME: silan and replaygain
        # gst-launch-1.0 -t filesrc location=02_Prada.mp3 ! decodebin !
        #  audioconvert ! audioresample ! rganalysis ! fakesink

        mutagenfile = mutagen.File(self._full_path(file_path), easy=True)
        self._replaygain_analysis(mutagenfile)
        self._silan_analysis(mutagenfile)
        mutagenfile.save()
        metadata = {
            'filename': filename,
            'path': file_path,
            'artist': mutagenfile.get('artist', [''])[0],
            'title': mutagenfile.get('title', [''])[0],
            'album': mutagenfile.get('album', [''])[0],
            'repeate': 1,
            'length': float(mutagenfile.info.length),
            'mtime': os.stat(self._full_path(file_path)).st_mtime,
        }
        return Response(json.dumps(metadata), mimetype='text/json')

    def on_update(self, request, category, filename):
        # FIXME: other values (artist, title)
        path = pjoin(category, secure_filename(filename))
        try:
            repeates = int(json.loads(request.data)['repeate'])
        except:  # noqa: E722
            raise UnprocessableEntity('Cannot parse PUT request')

        lines = open(self._full_path(category + '.m3u')).read().split('\n')
        with open(self._full_path(category + '.m3u'), 'w') as f:
            for line in lines:
                if line != path and line:
                    print(line, file=f)
            for i in range(repeates):
                print(path, file=f)
            del i

        return Response(json.dumps({'status': 'OK'}), mimetype='text/json')

    def on_delete(self, request, category, filename):
        path = pjoin(category, secure_filename(filename))
        if not os.path.exists(self._full_path(path)):
            raise NotFound()
        os.remove(self._full_path(path))
        lines = open(self._full_path(category + '.m3u')).read().split('\n')
        with open(self._full_path(category + '.m3u'), 'w') as f:
            for line in lines:
                if line != path and line:
                    print(line, file=f)
        return Response(json.dumps({'status': 'OK'}), mimetype='text/json')

    def on_static(self, request, path=''):
        if path in [''] + PLAYLISTS:
            path = 'index.html'
        path = os.path.join(self.static_dir, path)

        if path.endswith('.html'):
            mimetype = 'text/html'
        elif path.endswith('.css'):
            mimetype = 'text/css'
        elif path.endswith('.js'):
            mimetype = 'text/javascript'
        else:
            mimetype = 'text/plain'

        if not os.path.isfile(path):
            raise NotFound()

        return Response(wrap_file(request.environ, open(path, 'rb')),
                        mimetype=mimetype)


def import_files(playlist, files):
    pass


if __name__ == '__main__':
    import random
    from werkzeug.serving import run_simple
    data_dir = pjoin(os.path.dirname(os.path.abspath(__file__)), 'data')
    os.environ['KLANGBECKEN_DATA'] = data_dir
    os.environ['KLANGBECKEN_API_SECRET'] = \
        ''.join(random.sample('abcdefghijklmnopqrstuvwxyz', 20))
    for path in [data_dir] + [pjoin(data_dir, d) for d in PLAYLISTS]:
        if not os.path.isdir(path):
            os.mkdir(path)
    for path in [pjoin(data_dir, d + '.m3u') for d in PLAYLISTS]:
        if not os.path.isfile(path):
            open(path, 'a').close()

    if (len(sys.argv) > 3 and sys.argv[1] == '--import'
            and sys.argv[2] in PLAYLISTS):
        import_files(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) == 1:
        application = KlangbeckenAPI(stand_alone=True)

        # Inject dummy remote user when testing locally
        def wrapper(environ, start_response):
            environ['REMOTE_USER'] = 'dummyuser'
            return application(environ, start_response)

        run_simple('127.0.0.1', 5000, wrapper, use_debugger=True,
                   use_reloader=True, threaded=False)
    else:
        print("""${0}: Unknown command line arguments

Usage:
${0}: Run development server
${0} --import <${1}> FILES: Import files to playlist"""
              .format(sys.argv[0], '|'.join(PLAYLISTS)))
else:
    application = KlangbeckenAPI()
