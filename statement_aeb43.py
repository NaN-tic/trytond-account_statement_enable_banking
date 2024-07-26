# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import datetime
from trytond.pool import Pool, PoolMeta


class ImportStatement(metaclass=PoolMeta):
    __name__ = 'account.statement.import'

    def do_import_(self, action):
        pool = Pool()
        Statement = pool.get('account.statement')
        StatementOrigin = pool.get('account.statement.origin')

        action, data = super().do_import_(action)
        statement_ids = data.get('res_id', None)
        if statement_ids:
            statements = Statement.browse(statement_ids)
            origins = [o for s in statements for o in s.origins]
            # Get the suggested lines for each origin created
            StatementOrigin._search_reconciliation(origins)
        return action, data

    def aeb43_statement(self, account):
        statement = super().aeb43_statement(account)
        statement.start_date = datetime.combine(account.start_date,
            datetime.min.time())
        statement.end_date = datetime.combine(account.end_date,
            datetime.min.time())
        return statement

    def aeb43_origin(self, statement, transaction):
        origin, = super().aeb43_origin(statement, transaction)
        origin.state = 'registered'
        if statement and statement.journal:
            journal = statement.journal
            origin.number = journal.account_statement_origin_sequence.get()

        return [origin]
