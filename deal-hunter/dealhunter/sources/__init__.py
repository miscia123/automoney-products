from .auctions import Affide, Buyee, Catawiki, LiveAuctioneers, Zoll
from .base import Query, Source
from .chrono24 import Chrono24
from .ebay import Ebay
from .judicial import Judicial
from .subito import Subito
from .vinted import Vinted
from .wallapop import Wallapop
from .watches import (Kleinanzeigen, Marktplaats, OrologiPassioni, RedditWatchexchange, Ricardo,
                      WatchCollecting, Willhaben)

REGISTRY: dict[str, type[Source]] = {
    s.name: s for s in (Subito, Vinted, Ebay, Wallapop, Catawiki, Affide, Zoll, Buyee, Judicial, LiveAuctioneers,
                        Chrono24, Kleinanzeigen, Marktplaats, Willhaben, Ricardo, RedditWatchexchange,
                        WatchCollecting, OrologiPassioni)
}

__all__ = ["REGISTRY", "Query", "Source"]
