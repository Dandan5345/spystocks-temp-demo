"""Public family relationships; relatives are never labelled government officials."""
from copy import deepcopy

TRUMP_SOURCE = 'https://www.whitehouse.gov/administration/donald-j-trump/'
MELANIA_SOURCE = 'https://www.whitehouse.gov/administration/melania-trump/'
VANCE_SOURCE = 'https://www.whitehouse.gov/the-administration/usha-vance/'

CHILDREN = [('donald-trump-jr', 'Donald Trump Jr.'), ('ivanka-trump', 'Ivanka Trump'),
            ('eric-trump', 'Eric Trump'), ('tiffany-trump', 'Tiffany Trump'), ('barron-trump', 'Barron Trump')]

def relation(name, relationship, source, slug=None):
    return {'name': name, 'relationship': relationship, 'sourceUrl': source,
            'id': 'whitehouse:' + slug if slug else None}

def relatives(slug):
    if slug == 'donald-j-trump':
        return [relation('Melania Trump', 'Spouse', MELANIA_SOURCE, 'melania-trump')] + [
            relation(name, 'Child', TRUMP_SOURCE, child_slug) for child_slug, name in CHILDREN]
    if slug == 'melania-trump':
        return [relation('Donald J. Trump', 'Spouse', MELANIA_SOURCE, 'donald-j-trump'),
                relation('Barron Trump', 'Child', MELANIA_SOURCE, 'barron-trump')]
    if slug in {'jd-vance', 'usha-vance'}:
        spouse_slug, spouse = ('usha-vance', 'Usha Vance') if slug == 'jd-vance' else ('jd-vance', 'JD Vance')
        return [relation(spouse, 'Spouse', VANCE_SOURCE, spouse_slug)] + [
            relation(name + ' Vance', 'Child named in official biography', VANCE_SOURCE)
            for name in ['Ewan', 'Vivek', 'Mirabel']]
    if slug in dict(CHILDREN):
        return [relation('Donald J. Trump', 'Father', TRUMP_SOURCE, 'donald-j-trump')]
    return []

def family_index():
    return [{'id': 'whitehouse:' + slug, 'slug': slug, 'name': name,
             'role': 'Child of Donald J. Trump', 'profileType': 'family', 'sourceType': 'family',
             'currentOfficial': False, 'currentMember': False, 'imageUrl': None,
             'officialUrl': TRUMP_SOURCE} for slug, name in CHILDREN]

def family_profile(slug):
    item = next((item for item in family_index() if item['slug'] == slug), None)
    if item is None:
        return None
    return {**deepcopy(item), 'biography': [f"{item['name']} is one of Donald J. Trump's five children, as identified in his White House biography."],
            'family': relatives(slug), 'officialWebsiteUrl': TRUMP_SOURCE,
            'source': {'name': 'WhiteHouse.gov', 'officialUrl': TRUMP_SOURCE,
                       'description': 'Family relationship identified in the presidential biography', 'updatedAt': None}}
