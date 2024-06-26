# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from collections import defaultdict

from trytond.pool import Pool, PoolMeta
from trytond.tools import grouped_slice, reduce_ids
from trytond.transaction import Transaction


class Invoice(metaclass=PoolMeta):
    __name__ = 'account.invoice'

    # Need to refactorize the fleid function, to allow to have all
    # the statement lines from ana origin in the same account move.
    @classmethod
    def get_lines_to_pay(cls, invoices, name):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        AdditionalMove = pool.get('account.invoice-additional-account.move')
        line = MoveLine.__table__()
        invoice = cls.__table__()
        additional_move = AdditionalMove.__table__()
        cursor = Transaction().connection.cursor()

        lines = defaultdict(list)
        for sub_ids in grouped_slice(invoices):
            red_sql = reduce_ids(invoice.id, sub_ids)
            query = (invoice
                .join(line,
                    condition=((invoice.move == line.move)
                        & (invoice.account == line.account)
                        & (invoice.party == line.party)))
                .select(
                    invoice.id.as_('invoice'),
                    line.id.as_('line'),
                    line.maturity_date.as_('maturity_date'),
                    where=red_sql))
            query |= (invoice
                .join(additional_move,
                    condition=additional_move.invoice == invoice.id)
                .join(line,
                    condition=((additional_move.move == line.move)
                        & (invoice.account == line.account)
                        & (invoice.party == line.party)))
                .select(
                    invoice.id.as_('invoice'),
                    line.id.as_('line'),
                    line.maturity_date.as_('maturity_date'),
                    where=red_sql))
            cursor.execute(*query.select(
                    query.invoice, query.line,
                    order_by=query.maturity_date.nulls_last))
            for invoice_id, line_id in cursor:
                lines[invoice_id].append(line_id)
        return lines
