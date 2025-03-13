# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool, PoolMeta


class Move(metaclass=PoolMeta):
    __name__ = 'account.move'

    def get_allow_draft(self, name):
        pool = Pool()
        StatementOrigin = pool.get('account.statement.origin')

        if self.origin and isinstance(self.origin, StatementOrigin):
            return True
        return super().get_allow_draft(name)
