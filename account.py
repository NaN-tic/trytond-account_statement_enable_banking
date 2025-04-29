# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from sql import Column
from sql.aggregate import Max

from trytond.pool import Pool, PoolMeta
from trytond.model import fields
from trytond.tools import grouped_slice, reduce_ids
from trytond.transaction import Transaction
from trytond.modules.currency.fields import Monetary


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
    debit_credit_balance = fields.Function(Monetary(
        'Debit-Credit Balance', digits=(16, 2)),
        'get_debit_credit_balance')

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

    def get_debit_credit_balance(self, name):
        return self.debit - self.credit

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

    @classmethod
    def reconcile(cls, *lines_list, date=None, writeoff=None, description=None,
            delegate_to=None):
        pool = Pool()
        StatementLine = pool.get('account.statement.line')
        StatementSuggest = pool.get('account.statement.origin.suggested.line')

        # If are reocniling move lines that ara in some statement line related
        # or some suggested lines related. Remove the statement or suggested.
        lines = [line for lines in lines_list for line in lines]
        domain = [
            ('related_to', 'in', lines),
            ('origin.state', '!=', 'posted'),
            ]
        statement_lines = Transaction().context.get(
            'account_statement_lines', [])
        if statement_lines:
            domain.append(('id', 'not in', statement_lines))

        statement_lines_to_remove = StatementLine.search(domain)
        if statement_lines_to_remove:
            StatementLine.delete(statement_lines_to_remove)

        suggest_to_remove = StatementSuggest.search([
                ('related_to', 'in', lines),
                ('origin.state', '!=', 'posted'),
                ])
        if suggest_to_remove:
            StatementSuggest.delete(suggest_to_remove)

        return super().reconcile(*lines_list, date=date, writeoff=writeoff,
            description=description, delegate_to=delegate_to)
