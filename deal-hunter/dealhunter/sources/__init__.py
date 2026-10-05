from .auctions import Affide, Buyee, Catawiki, LiveAuctioneers, Zoll
from .base import Query, Source
from .ebay import Ebay
from .judicial import Judicial
from .subito import Subito
from .vinted import Vinted
from .wallapop import Wallapop

REGISTRY: dict[str, type[Source]] = {
    s.name: s for s in (Subito, Vinted, Ebay, Wallapop, Catawiki, Affide, Zoll, Buyee, Judicial, LiveAuctioneers)
}

__all__ = ["REGISTRY", "Query", "Source"]
