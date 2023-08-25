# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import datetime
import jwt as pyjwt

from trytond.config import config

def get_base_header():
    iat = int(datetime.now().timestamp())
    jwt_body = {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": iat,
            "exp": iat + 3600,
        }
    jwt = pyjwt.encode(
            jwt_body,
            open(config.get('enable_banking', 'keypath'), "rb").read(),
            algorithm='RS256',
            headers={'kid': config.get('enable_banking', 'applicationid')})
    base_headers = {"Authorization": f"Bearer {jwt}"}
    return base_headers
