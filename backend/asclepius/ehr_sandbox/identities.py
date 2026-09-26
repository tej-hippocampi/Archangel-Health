"""Seeded synthetic demographics, generated independently of any source identity."""
import calendar
import json
import random
import re
from datetime import date
from pathlib import Path
from .constants import ANCHOR

# Public-domain US Census frequency vocabulary, committed for offline replay.
_NAMES=json.loads(Path(__file__).with_name('names.json').read_text())
GIVEN=_NAMES['given']
FAMILY=_NAMES['family']


def identity(seed, index, age_band='60-69', sex='unknown', *, family=None, birth_year=None):
    rng=random.Random(f'{seed}:{index}:identity')
    band=re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)',str(age_band))
    older=re.fullmatch(r'(\d+)\+',str(age_band))
    lo,hi=(int(band[1]),int(band[2])) if band else (int(older[1]),int(older[1])+4) if older else (60,69)
    if not 0<=lo<=hi<=120: raise ValueError('invalid age band')
    age=rng.randint(lo,hi)
    month=rng.randint(1,12)
    year=birth_year or ANCHOR.year-age-(month>ANCHOR.month)
    day=rng.randint(1,calendar.monthrange(year,month)[1])
    if birth_year is None and month==ANCHOR.month and day>ANCHOR.day: year-=1
    if birth_year is not None:
        # A confusable birth year must still agree with the source age band.
        choices=[]
        for m in range(1,13):
            for d in range(1,calendar.monthrange(birth_year,m)[1]+1):
                candidate_age=ANCHOR.year-birth_year-((m,d)>(ANCHOR.month,ANCHOR.day))
                if lo<=candidate_age<=hi: choices.append((m,d))
        if not choices: raise ValueError('incompatible_decoy_age_band')
        month,day=rng.choice(choices)
    given=rng.choice(GIVEN); family=family or rng.choice(FAMILY)
    return {'name':[{'use':'official','family':family,'given':[given],'text':given+' '+family}],
            'identifier':[{'system':'https://archangel.health/synthetic-mrn','value':str(10000000+(int(seed)*97+index*7919)%90000000)}],
            'birthDate':date(year,month,day).isoformat(),'gender':sex}
