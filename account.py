# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from sql import Column
from sql.aggregate import Max

from trytond.pool import Pool, PoolMeta
from trytond.model import fields
from trytond.tools import grouped_slice, reduce_ids
from trytond.transaction import Transaction


class Move(metaclass=PoolMeta):
    __name__ = 'account.move'

    @classmethod
    def _get_origin(cls):
        return super()._get_origin() + ['account.statement.origin']


class MoveLine(metaclass=PoolMeta):
    __name__ = 'account.move.line'

    payment_group = fields.Function(fields.Many2One('account.payment.group',
            'Payment Group'),
        'get_payment_fields', searcher='search_payment_group')
    payment_date = fields.Function(fields.Date('Payment Date'),
        'get_payment_fields', searcher='search_payment_date')

    @classmethod
    def get_payment_fields(cls, lines, name):
        pool = Pool()
        Payment = pool.get('account.payment')
        table = Payment.__table__()
        cursor = Transaction().connection.cursor()

        line_ids = [l.id for l in lines]
        result = {}.fromkeys(line_ids, None)

        for sub_ids in grouped_slice(line_ids):
            query = table.select(table.line, Max(Column(table, name[8:])),
                where=((table.state != 'failed')
                    & reduce_ids(table.line, sub_ids)), group_by=table.line)
            cursor.execute(*query)
            result.update(dict(cursor.fetchall()))
        return result

    @classmethod
    def search_payment_group(cls, name, clause):
        return [('payments.group.rec_name',) + tuple(clause[1:])]

    @classmethod
    def search_payment_date(cls, name, clause):
        return [('payments.date',) + tuple(clause[1:])]

    @classmethod
    def _get_origin(cls):
        return super()._get_origin() + ['account.statement.origin']

    @classmethod
    def check_modify(cls, *args, **kwargs):
        # It's needed to modify the lines even if the move is in 'posted'
        # state.
        if Transaction().context.get('from_account_statement_origin',
                False):
            return
        return super().check_modify(*args, **kwargs)
