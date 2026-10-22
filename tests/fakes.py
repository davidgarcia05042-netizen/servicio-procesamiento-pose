"""
Dobles de prueba compartidos.

`FakeSession` reemplaza la sesión de SQLAlchemy en los tests de consultas: no
toca PostgreSQL, guarda las sentencias que recibe para poder compilarlas y
revisar el SQL, y devuelve las filas que se le indiquen. Antes había tres
copias casi idénticas en test_stuck_jobs, test_data_retention y
test_callback_retry.
"""


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return len(self._rows)


class FakeSession:
    def __init__(self, rows=()):
        self._result = FakeResult(rows)
        self.statements = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self._result

    async def commit(self):
        self.commits += 1
