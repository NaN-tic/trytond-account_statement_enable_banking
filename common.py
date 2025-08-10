# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import datetime
import jwt as pyjwt
from urllib.parse import urlparse

from trytond.config import config

KEYPATH = config.get('enable_banking', 'keypath')
URL = config.get('enable_banking', 'api_origin', default='https://sandbox.enablebanking.com')
APPLICATION_ID = config.get('enable_banking', 'applicationid')
REDIRECT_URL = config.get('enable_banking', 'redirecturl')


def get_base_header():
    if not KEYPATH:
        return {}
    iat = int(datetime.now().timestamp())
    jwt_body = {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": iat,
            "exp": iat + 86400,
        }
    jwt = pyjwt.encode(jwt_body, open(KEYPATH, "rb").read(), algorithm='RS256',
        headers={'kid': APPLICATION_ID})

    host = urlparse(URL).netloc
    base_headers = {
        "Host": host,
        "Accept": "application/json",
        "Psu-Ip-Address": "172.17.0.254",
        "Psu-User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        #"Psu-Referer": # PSU Referer
        "Psu-Accept": "application/json", # PSU accept header
        #"Psu-Accept-Charset": 'utf-8', # PSU charset
        #"Psu-Accept-Encoding": # PSU accept encoding
        #"Psu-Accept-language": # PSU accept language
        #"Psu-Geo-Location":	# Comma separated latitude and longitude coordinates without spaces
        "Authorization": f"Bearer {jwt}",
        }
    return base_headers
