"""A local stand-in for Google Places, used for dry runs and the smoke test.

It serves the two Places endpoints plus the business websites they point at, so the
whole pipeline can be exercised without an API key and without spending any quota.

    python mock_places_server.py
    PLACES_BASE_URL=http://127.0.0.1:8899 GOOGLE_API_KEY=mock-key \
        python medspa_scraper.py --zip 90210 90211

Every business is derived from the requested zip code, so results stay reproducible.
"""

import argparse
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8899

PAGE_SIZE = 2

# One template per situation the pipeline has to cope with. Addresses on a .invalid
# domain are meant to have no MX records, which is how a lead ends up rejected.
BUSINESS_TEMPLATES = [
    {'slug': 'glow', 'name': 'Glow Med Spa', 'has_site': True, 'email': 'hello@glow-spa.example'},
    {'slug': 'radiance', 'name': 'Radiance Skin Clinic', 'has_site': True, 'email': None},
    {'slug': 'serenity', 'name': 'Serenity Facial Bar', 'has_site': False, 'email': None},
    {'slug': 'closed', 'name': 'Closed Aesthetics', 'has_site': True, 'email': None, 'closed': True},
    {'slug': 'nophone', 'name': 'Quiet Esthetics', 'has_site': True, 'email': 'book@quiet.example', 'phone': None},
    {'slug': 'bounce', 'name': 'Bounce Aesthetics', 'has_site': True, 'phone': None,
     'email': 'owner@bounce.invalid'},
]


def businesses_for(zip_code):
    """Build the full, deterministic business list for one zip code."""
    businesses = []
    for index, template in enumerate(BUSINESS_TEMPLATES):
        slug = f"{zip_code}-{template['slug']}"
        businesses.append({
            'slug': slug,
            'place_id': f'mock-place-{slug}',
            'name': f"{template['name']} {zip_code}",
            'address': f"{100 + index} Mock Blvd, Beverly Hills, CA {zip_code}, USA",
            'phone': template.get('phone', f'(310) 555-{1000 + index:04d}'),
            'has_site': template['has_site'],
            'email': template['email'],
            'closed': template.get('closed', False),
        })
    return businesses


def find_business(place_id):
    # The zip code is embedded in the slug, so the list can be rebuilt from the id
    zip_code = place_id.replace('mock-place-', '').split('-')[0]
    for business in businesses_for(zip_code):
        if business['place_id'] == place_id:
            return business
    return None


def website_url(request, business):
    return f"{request.scheme}://{request.host}/site/{business['slug']}"


def zip_from_query(query):
    """Pull the zip code out of a query such as 'Med Spa in 90210, CA'."""
    for token in query.replace(',', ' ').split():
        if token.isdigit() and len(token) == 5:
            return token
    return '00000'


async def handle_search(request):
    query = request.query.get('query', '')
    page_token = request.query.get('pagetoken')

    zip_code = zip_from_query(page_token.replace('page2-', '') if page_token else query)
    businesses = businesses_for(zip_code)

    if page_token:
        page, next_token = businesses[PAGE_SIZE:], None
    else:
        page, next_token = businesses[:PAGE_SIZE], f'page2-{zip_code}'

    payload = {
        'status': 'OK',
        'results': [
            {
                'name': business['name'],
                'place_id': business['place_id'],
                'formatted_address': business['address'],
                'business_status': 'CLOSED_PERMANENTLY' if business['closed'] else 'OPERATIONAL',
            }
            for business in page
        ],
    }
    if next_token:
        payload['next_page_token'] = next_token

    logger.info("search %r page %s -> %s results", query, 2 if page_token else 1, len(payload['results']))
    return web.json_response(payload)


async def handle_details(request):
    place_id = request.query.get('place_id', '')
    business = find_business(place_id)
    if business is None:
        return web.json_response({'status': 'NOT_FOUND'})

    result = {
        'name': business['name'],
        'formatted_address': business['address'],
    }
    if business['phone']:
        result['formatted_phone_number'] = business['phone']
    if business['has_site']:
        result['website'] = website_url(request, business)

    logger.info("details %s", place_id)
    return web.json_response({'status': 'OK', 'result': result})


async def handle_site(request):
    slug = request.match_info['slug']
    zip_code = slug.split('-')[0]
    for business in businesses_for(zip_code):
        if business['slug'] == slug:
            contact = f"<p>Email: {business['email']}</p>" if business['email'] else '<p>Call us today.</p>'
            return web.Response(
                text=f"<html><body><h1>{business['name']}</h1>{contact}</body></html>",
                content_type='text/html',
            )
    return web.Response(status=404, text='Not found')


def create_app():
    app = web.Application()
    app.router.add_get('/textsearch/json', handle_search)
    app.router.add_get('/details/json', handle_details)
    app.router.add_get('/site/{slug}', handle_site)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a local stand-in for the Google Places API.")
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    logger.setLevel(logging.INFO)

    base_url = f'http://{args.host}:{args.port}'
    logger.info("Mock Places API listening on %s", base_url)
    logger.info("Point the scraper at it with PLACES_BASE_URL=%s", base_url)

    web.run_app(create_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
