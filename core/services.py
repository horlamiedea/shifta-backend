class BaseService:
    """
    Base class for Services (Write operations).
    """
    def __call__(self, *args, **kwargs):
        raise NotImplementedError("Services must implement the __call__ method.")

class BaseSelector:
    """
    Base class for Selectors (Read operations).
    """
    def __call__(self, *args, **kwargs):
        raise NotImplementedError("Selectors must implement the __call__ method.")
