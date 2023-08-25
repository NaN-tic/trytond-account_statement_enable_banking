# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from werkzeug.wrappers import Response
import requests

from trytond.protocols.wrappers import with_pool, with_transaction, allow_null_origin
from trytond.transaction import Transaction
from trytond.wsgi import app
from trytond.config import config
from .common import get_base_header



@app.route('/<database_name>/enable_banking/redirect')
@allow_null_origin
@with_pool
@with_transaction(readonly=False)
def redirect(request, pool):
    EBSession = pool.get('enable_banking.session')
    EBSessionReportOK = pool.get('enable_banking.session_ok', type='report')
    EBSessionReportKO = pool.get('enable_banking.session_ko', type='report')
    if 'code' in request.args.keys():
        auth_code = request.args['code']
    base_headers = get_base_header()

    r = requests.post(f"{config.get('enable_banking', 'api_origin')}/sessions",
        json={"code": auth_code}, headers=base_headers)

    data = {'model': EBSession.__name__}

    #TODO: hot to handle code 422
    if r.status_code == 200:
        session = r.json()

        eb_session = EBSession.search([('session_id', '=', request.args['state'])], limit=1)
        if not eb_session:
            ext, content, _, _ = EBSessionReportKO.execute([], data)

        eb_session = eb_session[0]
        with Transaction().set_context(company=eb_session.company.id):
            #eb_session.valid_until = datetime.strptime(session['access']['valid_until'], '%Y-%m-%dT%H:%M:%S.%f%z')
            eb_session.session = session
            EBSession.save([eb_session])
            ext, content, _, _ = EBSessionReportOK.execute([], data)
    else:
        ext, content, _, _ = EBSessionReportKO.execute([], data)

    assert ext == 'html'
    return Response(content, 200, content_type='text/html')
