# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import datetime
from trytond.pool import PoolMeta


class ImportStatement(metaclass=PoolMeta):
    __name__ = 'account.statement.import'

    def aeb43_statement(self, account):
        statement = super().aeb43_statement(account)
        statement.start_date = datetime.combine(account.initialDate,
            datetime.min.time())
        statement.end_date = datetime.combine(account.finalDate,
            datetime.min.time())
        return statement

    def aeb43_origin(self, statement, transaction):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')

        origin, = super().aeb43_origin(statement, transaction)
        origin.state = 'registered'
        if origin.number is None and statement and statement.journal:
            journal = statement.journal
            origin.number = journal.account_statement_origin_sequence.get()

        origins = [origin]
        # Get the suggested lines for each origin created
        StatementOrigin._search_reconciliation(origins)

        return origins
