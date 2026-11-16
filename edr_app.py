# =================================================================
#
# Authors: Shane Mill <shane.mill@noaa.gov>
#
# Copyright (c) 2026 Shane Mill
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

"""WSGI entry point: pygeoapi plus a file download endpoint.

Run with ``gunicorn edr_app:APP`` (instead of ``pygeoapi.flask_app:APP``).

Adds, without modifying pygeoapi::

    GET|HEAD /collections/{collectionId}/items/{itemId}/download

The item's ``enclosure`` link points here. The bytes are read through the
collection's ``FileListingProvider`` (http, local disk or s3 via fsspec) and
streamed with ``Content-Disposition: attachment``. Single-range requests are
supported.
"""

import logging
import re
from email.utils import format_datetime

from flask import Response, abort, request

from pygeoapi.flask_app import API_RULES, APP, CONFIG
from pygeoapi.plugin import load_plugin
from pygeoapi.provider.base import (ProviderConnectionError,
                                    ProviderItemNotFoundError)

LOGGER = logging.getLogger(__name__)

RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')
_providers = {}


def _get_provider(collection_id):
    """Return the collection's file provider, or None"""
    if collection_id not in _providers:
        provider = None
        resource = CONFIG.get('resources', {}).get(collection_id)
        if resource and resource.get('type') == 'collection':
            for prov in resource.get('providers', []):
                if prov.get('type') == 'feature':
                    candidate = load_plugin('provider', prov)
                    if hasattr(candidate, 'open_item'):
                        provider = candidate
                    break
        _providers[collection_id] = provider
    return _providers[collection_id]


def _safe_filename(name):
    return re.sub(r'[^A-Za-z0-9._-]', '_', name)


def _parse_range(header, size):
    """
    Parse a single ``bytes=a-b`` range.

    :returns: (start, end) inclusive, ``None`` if no/ignored range
    :raises ValueError: if unsatisfiable
    """
    if not header or size is None:
        return None
    m = RANGE_RE.match(header.strip())
    if not m or (m.group(1) == '' and m.group(2) == ''):
        return None  # malformed or multi-range: ignore, send everything
    if m.group(1) == '':  # suffix range: last N bytes
        n = int(m.group(2))
        if n == 0:
            raise ValueError
        return max(size - n, 0), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        raise ValueError
    return start, min(end, size - 1)


@APP.route(f'{API_RULES.get_url_prefix("flask")}/collections/'
           '<path:collection_id>/items/<path:item_id>/download',
           methods=['GET', 'HEAD'])
def download_item(collection_id, item_id):
    provider = _get_provider(collection_id)
    if provider is None:
        abort(404)
    try:
        item = provider.open_item(item_id)
    except ProviderItemNotFoundError:
        abort(404)
    except ProviderConnectionError as err:
        LOGGER.error(f'Storage error for {collection_id}/{item_id}: {err}')
        abort(502)

    size = item['size']
    headers = {
        'Content-Type': item['media_type'],
        'Content-Disposition':
            f'attachment; filename="{_safe_filename(item["name"])}"',
        'Accept-Ranges': 'bytes' if size is not None else 'none',
        'Cache-Control': 'public, max-age=3600',
    }
    if item['modified']:
        headers['Last-Modified'] = format_datetime(item['modified'],
                                                   usegmt=True)
        headers['ETag'] = f'"{int(item["modified"].timestamp())}-{size}"'

    try:
        byte_range = _parse_range(request.headers.get('Range'), size)
    except ValueError:
        return Response(status=416, headers={
            **headers, 'Content-Range': f'bytes */{size}'})

    status = 200
    start, end = 0, None
    if byte_range:
        status = 206
        start, end = byte_range
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        headers['Content-Length'] = str(end - start + 1)
    elif size is not None:
        headers['Content-Length'] = str(size)

    if request.method == 'HEAD':
        return Response(status=status, headers=headers)

    return Response(provider.iter_bytes(item, start, end), status=status,
                    headers=headers, direct_passthrough=True)


def _prewarm():
    """Start loading every collection's file listing in the background"""
    for collection_id in CONFIG.get('resources', {}):
        try:
            _get_provider(collection_id)
        except Exception as err:  # never block startup
            LOGGER.warning(f'Prewarm failed for {collection_id}: {err}')


_prewarm()
