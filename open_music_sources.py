"""On-demand discovery for public music catalogues. No bulk URL snapshots."""
import json
import os
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

GENRES = ('', 'Classical', 'Electronic', 'Jazz', 'Rock', 'Ambient', 'Folk')
SITES = {
    'fma': 'https://freemusicarchive.org/search/',
    'musopen': 'https://musopen.org/music/',
}


def site_url(provider, query='', genre=''):
    base = SITES[provider]
    if provider == 'fma':
        return base + '?' + urlencode({'quicksearch': query or genre, 'adv': '1'})
    return base + '?' + urlencode({'q': query or genre})


def _json(url, params=None):
    target = url + ('?' + urlencode(params, doseq=True) if params else '')
    request = Request(target, headers={'User-Agent': 'VeliaMusicPlayer/1.0 (personal music discovery)'})
    with urlopen(request, timeout=12) as response:
        return json.load(response)


def search_archive(query, genre='', page=1, limit=24):
    terms = ['mediatype:audio', '(licenseurl:* OR rights:* OR collection:opensource_audio)']
    if query.strip():
        safe = query.strip().replace('"', ' ').replace(':', ' ')
        terms.append('(' + ' AND '.join(word for word in safe.split()[:7]) + ')')
    if genre:
        terms.append('subject:' + genre.lower())
    data = _json('https://archive.org/advancedsearch.php', {
        'q': ' AND '.join(terms), 'fl[]': ['identifier', 'title', 'creator', 'year'],
        'rows': limit, 'page': max(1, page), 'output': 'json',
    })
    docs = data.get('response', {}).get('docs', [])
    return [{'provider': 'archive', 'id': d['identifier'], 'title': d.get('title') or d['identifier'],
             'artist': d.get('creator', ''), 'year': d.get('year', ''),
             'url': 'https://archive.org/details/' + quote(d['identifier'])}
            for d in docs if d.get('identifier')]


def archive_audio(identifier):
    info = _json('https://archive.org/metadata/' + quote(identifier, safe=''))
    metadata = info.get('metadata', {})
    files = [f for f in info.get('files', []) if not f.get('private') and
             f.get('name', '').lower().endswith(('.mp3', '.ogg', '.flac', '.m4a', '.wav'))]
    files.sort(key=lambda f: (f.get('name', '').lower().endswith('.mp3'), f.get('source') == 'original'), reverse=True)
    return [{'provider': 'archive', 'id': identifier + '/' + f['name'],
             'title': f['name'].rsplit('.', 1)[0].replace('_', ' '),
             'artist': metadata.get('creator', ''), 'album': metadata.get('title', ''),
             'year': metadata.get('year', ''), 'url': 'https://archive.org/download/' + quote(identifier, safe='') + '/' + quote(f['name']),
             'detail_url': 'https://archive.org/details/' + quote(identifier)} for f in files[:80]]


def search_jamendo(query, genre='', page=1, limit=24, client_id=''):
    client_id = client_id.strip() or os.environ.get('VELIA_JAMENDO_CLIENT_ID', '').strip()
    if not client_id:
        raise ValueError('Jamendo client ID required / 需要在设置中填写 Jamendo client ID')
    params = {'client_id': client_id, 'format': 'json', 'limit': limit,
              'offset': (max(1, page) - 1) * limit, 'include': 'musicinfo',
              'audioformat': 'mp32'}
    if query.strip():
        params['search'] = query.strip()
    if genre:
        params['tags'] = genre.lower()
    data = _json('https://api.jamendo.com/v3.0/tracks/', params)
    if data.get('headers', {}).get('status') != 'success':
        raise ValueError(data.get('headers', {}).get('error_message') or 'Jamendo API error')
    return [{'provider': 'jamendo', 'id': str(t['id']), 'title': t.get('name', ''),
             'artist': t.get('artist_name', ''), 'album': t.get('album_name', ''),
             'year': (t.get('releasedate') or '')[:4], 'cover': t.get('image', ''),
             'url': t.get('audio', ''), 'download': t.get('audiodownload', '') if t.get('audiodownload_allowed') else '',
             'detail_url': t.get('shareurl', ''), 'license': t.get('license_ccurl', '')}
            for t in data.get('results', []) if t.get('audio')]
