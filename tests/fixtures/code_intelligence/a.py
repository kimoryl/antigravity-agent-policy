import b


class Alpha:
    def caller(self):
        return b.target()


def duplicate():
    return "a"


def dynamic(factory):
    return getattr(factory, "run")()
