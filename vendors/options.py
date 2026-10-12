# Kept in step with frontend/src/lib/options.ts.
CATEGORIES = [
    'Pets Accessories', 'Arts, Prints & Stickers', 'Beauty & Cosmetics', 'Ceramics / Glass', 'Clothing Brand',
    'Crotchet, Sewing, Quilt Goods', 'Cultural Collectibles', 'Experience', 'Homeware', 'Jewelry', 'Leatherwork',
    'Music / Vinyl', 'NYC Merch', 'Photography', 'Plants', 'Soaps / Candles and Perfume', 'Stationery',
    'Unique Things', 'Vintage Clothing', 'Vintage General', 'Vintage Homeware', 'Vintage Jewelry', 'Vintage Upcycled',
]
LEAD_SOURCES = [
    'Instagram', 'Email', 'Shopify popup form', 'Shopify lead form', 'Meta ads', 'Application form', 'CSV import',
]


def canonical(options: list[str], value: str) -> str | None:
    wanted = value.strip().lower()
    return next((option for option in options if option.lower() == wanted), None)
