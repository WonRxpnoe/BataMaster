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
    {'slug': 'nophone', 'name': 'Quiet Esthetics', 'has_site': True, 'phone': None,
     'email': 'book@quiet.example'},
    {'slug': 'bounce', 'name': 'Bounce Aesthetics', 'has_site': True, 'phone': None,
     'email': 'owner@bounce.invalid'},
    # Its address is printed on a contact page the homepage links to, not on the homepage
    {'slug': 'hidden', 'name': 'Hidden Contact Spa', 'has_site': True, 'phone': None,
     'email': 'owner@hidden-spa.example', 'email_on_contact_page': True},
    # Its homepage also carries an asset filename and the address of whoever built it
    {'slug': 'noisy', 'name': 'Noisy Site Spa', 'has_site': True, 'phone': None,
     'email': 'owner@noisy-spa.example', 'noisy_site': True},
    # The same shop as 'glow', which Google holds a second, separate entry for
    {'slug': 'twin', 'name': 'Glow Med Spa LLC', 'has_site': True, 'email': None,
     'duplicate_of': 'glow'},
]


def businesses_for(zip_code):
    """Build the full, deterministic business list for one zip code."""
    businesses = []
    by_slug = {}
    for index, template in enumerate(BUSINESS_TEMPLATES):
        slug = f"{zip_code}-{template['slug']}"
        business = {
            'slug': slug,
            'place_id': f'mock-place-{slug}',
            'name': f"{template['name']} {zip_code}",
            'address': f"{100 + index} Mock Blvd, Beverly Hills, CA {zip_code}, USA",
            'phone': template.get('phone', f'(310) 555-{1000 + index:04d}'),
            'has_site': template['has_site'],
            'email': template['email'],
            'closed': template.get('closed', False),
            'email_on_contact_page': template.get('email_on_contact_page', False),
            'noisy_site': template.get('noisy_site', False),
        }
        # A second entry for one shop repeats its address and phone, which is what makes
        # the pair recognisable as a single business further down the pipeline
        original = by_slug.get(template.get('duplicate_of'))
        if original:
            business.update(address=original['address'], phone=original['phone'])
        by_slug[template['slug']] = business
        businesses.append(business)
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

    # A page token carries no query, so log the zip code it resolved to instead
    logger.info(
        "search %s page %s -> %s results",
        repr(query) if query else f'(token for {zip_code})',
        2 if page_token else 1,
        len(payload['results']),
    )
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


def site_body(business, on_contact_page=False):
    """The homepage a business serves, or its contact page when asked for that."""
    heading = f"<h1>{business['name']}</h1>"

    if business['email_on_contact_page'] and not on_contact_page:
        return f"{heading}<a href='/site/{business['slug']}/contact'>Contact</a>"

    if not business['email']:
        return f'{heading}<p>Call us today.</p>'

    if business['noisy_site']:
        # Everything a real page puts in front of the address a visitor is looking for
        domain = business['email'].split('@')[1]
        return (
            f'{heading}<img src="logo@2x.PNG">'
            '<!-- built by support@squarespace.com -->'
            '<script>Sentry.init({dsn:"https://k@o1.ingest.sentry.io/4"});'
            # On the business's own domain, so only ignoring scripts can rule it out
            f'var cfg={{notify:"no-reply@{domain}"}};</script>'
            '<input placeholder="you@email.com">'
            f"<p>Email: {business['email']}</p>"
        )

    return f"{heading}<p>Email: {business['email']}</p>"


def find_by_slug(slug):
    zip_code = slug.split('-')[0]
    for business in businesses_for(zip_code):
        if business['slug'] == slug:
            return business
    return None


async def handle_site(request):
    business = find_by_slug(request.match_info['slug'])
    if business is None:
        return web.Response(status=404, text='Not found')
    return web.Response(
        text=f"<html><body>{site_body(business)}</body></html>",
        content_type='text/html',
    )


async def handle_contact_page(request):
    business = find_by_slug(request.match_info['slug'])
    if business is None:
        return web.Response(status=404, text='Not found')
    return web.Response(
        text=f"<html><body>{site_body(business, on_contact_page=True)}</body></html>",
        content_type='text/html',
    )


def create_app():
    app = web.Application()
    app.router.add_get('/textsearch/json', handle_search)
    app.router.add_get('/details/json', handle_details)
    app.router.add_get('/site/{slug}', handle_site)
    app.router.add_get('/site/{slug}/contact', handle_contact_page)
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
