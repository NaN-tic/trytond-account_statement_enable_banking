# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.pool import Pool, PoolMeta


class CompensationMove(metaclass=PoolMeta):
    __name__ = 'account.move.compensation_move'

    def get_extra_lines(self, lines, account, party=None):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        StatementOrigin = pool.get('account.statement.origin')

        extra_lines, origin = super().get_extra_lines(lines, account, party)
        if (isinstance(origin, StatementLine)
                or isinstance(origin, StatementOrigin)):
            origin = None
        return extra_lines, origin
