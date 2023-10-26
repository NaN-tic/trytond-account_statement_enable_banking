# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.pool import PoolMeta
from trytond.transaction import Transaction


class Move(metaclass=PoolMeta):
    __name__ = 'account.move'

    @classmethod
    def _get_origin(cls):
        return super()._get_origin() + ['account.statement.origin']


class MoveLine(metaclass=PoolMeta):
    __name__ = 'account.move.line'

    @classmethod
    def _get_origin(cls):
        return super()._get_origin() + ['account.statement.origin']

    @classmethod
    def check_modify(cls, *args, **kwargs):
        context = Transaction().context
        # We need to modify the lines even if the move is in 'posted' state.
        if Transaction().context.get('from_account_statement_origin',
                False):
            return
        return super().check_modify(*args, **kwargs)
