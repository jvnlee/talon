class TalonError(Exception):
    pass


class SourceError(TalonError):
    pass


class SchemaDriftError(SourceError):
    pass
