"""Tests for dashboard sign-in persistence.

The load-bearing property is that "Keep me signed in" survives closing the
browser AND that a returning visitor is not shown the sign-in form anyway.
Those are two separate failures that look identical from the outside: the
cookie was already persistent and correct while `/` rendered the login page
regardless of session state, so the feature read as broken when it was not.

Also pins logout to POST. As a GET link, any prefetcher following the href
(Chrome's "Preload pages", extensions, link scanners) silently ended the
session — the same symptom, from the opposite direction.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# app.py imports executor -> py_clob_client_v2, absent in the test env.
for mod in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client_v2.client"].ClobClient = object
_ct = sys.modules["py_clob_client_v2.clob_types"]
for _n in ("MarketOrderArgsV2", "OrderArgsV2", "OrderType", "ApiCreds", "BalanceAllowanceParams", "AssetType"):
    if not hasattr(_ct, _n):
        setattr(_ct, _n, object)


@pytest.fixture
def client():
    import app as A
    A.app.config.update(TESTING=True)
    return A, A.app.test_client()


def _login(A, c, remember):
    return c.post('/api/login', json={'email': A.DASHBOARD_EMAIL,
                                      'password': A.DASHBOARD_PASSWORD,
                                      'remember': remember})


def test_remember_me_issues_a_persistent_cookie(client):
    A, c = client
    r = _login(A, c, True)
    assert r.status_code == 200
    cookie = r.headers.get('Set-Cookie', '')
    # A browser only keeps the cookie across a restart if it carries an expiry.
    assert 'Expires=' in cookie or 'Max-Age=' in cookie, cookie


def test_without_remember_the_cookie_dies_with_the_browser(client):
    A, c = client
    r = _login(A, c, False)
    assert r.status_code == 200
    cookie = r.headers.get('Set-Cookie', '')
    assert 'Expires=' not in cookie and 'Max-Age=' not in cookie, cookie


def test_session_lifetime_is_thirty_days(client):
    A, _ = client
    # Assigned as an attribute in app.py; assert the CONFIG value, which is what
    # Flask actually enforces when it reads the cookie back.
    assert A.app.config['PERMANENT_SESSION_LIFETIME'].days == 30


def test_landing_page_lets_a_signed_in_visitor_through(client):
    """The regression: a valid session must not be shown the sign-in form."""
    A, c = client
    _login(A, c, True)
    r = c.get('/')
    assert r.status_code == 302, "signed-in visitor was served the login page"
    assert r.headers['Location'].endswith('/dashboard')


def test_landing_page_still_serves_login_when_signed_out(client):
    A, c = client
    r = c.get('/')
    assert r.status_code == 200
    assert b'Keep me signed in' in r.data


def test_logout_is_not_reachable_by_get(client):
    """A GET logout is destroyed by link prefetchers and cross-site links."""
    A, c = client
    _login(A, c, True)
    # 404 rather than 405: with no GET rule the path falls through to the
    # static catch-all. Either way it must not succeed...
    assert c.get('/api/logout').status_code >= 400
    # ...and the load-bearing part is that the session survived the attempt.
    assert c.get('/').status_code == 302


def test_logout_by_post_ends_the_session(client):
    A, c = client
    _login(A, c, True)
    assert c.post('/api/logout').status_code == 200
    assert c.get('/').status_code == 200  # back to the sign-in form
