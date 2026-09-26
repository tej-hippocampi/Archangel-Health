"""Versioned offline vocabulary; unmapped concepts retain their source text."""
import json
import re
from pathlib import Path

LOINC = 'http://loinc.org'
RXNORM = 'http://www.nlm.nih.gov/research/umls/rxnorm'
ICD10 = 'http://hl7.org/fhir/sid/icd-10-cm'
GROUPS = {
    'K': {'2823-3'}, 'creatinine': {'2160-0', '38483-4'}, 'eGFR': {'33914-3', '48642-3', '62238-1', '98979-8'},
    'BMP': {'24323-8', '2823-3', '2160-0', '2951-2', '2075-0', '2028-9', '3094-0', '2345-7', '17861-6'},
    'CMP': {'24323-8','24325-3', '2823-3', '2160-0', '2951-2', '2075-0', '2028-9', '3094-0', '2345-7', '17861-6', '1751-7', '1742-6', '1920-8'},
    'renal': {'24362-6','2823-3','2160-0','2951-2','2075-0','2028-9','3094-0','2345-7','17861-6','1751-7','2777-1','33914-3'},
    'UACR': {'9318-7','14959-1'}, 'CBC': {'58410-2','718-7','6690-2','777-3'},
    'PTH': {'2731-8'}, 'phosphorus': {'2777-1'}, 'vitamin_d': {'1989-3'},
    'iron': {'2498-4','2502-3','2276-4','2500-7'}, 'lipid': {'24331-1','2093-3','2085-9','13457-7'},
    'A1c': {'4548-4'}, 'urinalysis': {'24356-8'},
}
LAB_NAMES = {'creatinine': '2160-0', 'potassium': '2823-3', 'k': '2823-3', 'egfr': '33914-3',
             'uacr': '9318-7', 'sodium': '2951-2', 'phosphorus': '2777-1', 'hemoglobin': '718-7',
             'albumin': '1751-7', 'calcium': '17861-6', 'bicarbonate': '2028-9'}


def normalize(text):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9.]+', ' ', str(text).lower())).strip()


def drug(text):
    cache = json.loads(Path(__file__).with_name('terminology_cache.json').read_text())
    words = normalize(text)
    for item in cache['drugs']:
        if any(words == normalize(alias) for alias in item['aliases']):
            return {'name': item['name'], 'ingredient': item['ingredient'], 'mapped': bool(item['ingredient'])}
    return {'name': words, 'ingredient': None, 'mapped': False}


def lab_code(name):
    return LAB_NAMES.get(normalize(name))


def medication_span_supported(item):
    """Conservative lexical support for corrected medication decisions."""
    span=normalize(item.get('source_span','')); mapped=drug(item.get('drug',''))
    cache=json.loads(Path(__file__).with_name('terminology_cache.json').read_text())
    aliases=next((d['aliases'] for d in cache['drugs'] if d['name']==mapped['name']),[item.get('drug','')])
    if not any(re.search(r'\b'+re.escape(normalize(a))+r'\b',span) for a in aliases if a): return False
    verbs={'start':'start|begin|add|initiate','stop':'stop|discontinue|withdraw','hold':'hold|withhold',
           'increase':'increase|raise|up titrate|uptitrate','decrease':'decrease|reduce|lower','change':'change|switch'}
    if not re.search(r'\b(?:'+verbs.get(item.get('action'),'(?!)')+r')\b',span): return False
    if re.search(r'\b(?:not|never|avoid|no)\s+(?:\w+\s+){0,2}(?:'+verbs.get(item.get('action'),'(?!)')+r')\b',span): return False
    amount=daily_amount(item.get('to_dose'))
    if amount is not None:
        # In "from 20 to 10", only the destination supports a new-dose claim.
        dose_span=re.split(r'\bto\b',item.get('source_span',''),flags=re.I)[-1]
        parsed=daily_amount(dose_span)
        if parsed is None or parsed['unit']!=amount['unit'] or abs(parsed['value']-amount['value'])>=.0001: return False
    return True


def lab_group(text):
    norm = normalize(text)
    aliases = {'basic metabolic panel':'BMP', 'comprehensive metabolic panel':'CMP', 'renal panel':'renal',
               'renal function panel':'renal', 'potassium':'K', 'urine albumin creatinine ratio':'UACR'}
    if norm in aliases:
        return aliases[norm]
    return next((k for k,v in GROUPS.items() if norm == k.lower() or str(text) in v), None)


def daily_dose(text, frequency=None):
    """Mass dose in mg/day; other dimensions cannot feed renal mg ceilings."""
    amount=daily_amount(text,frequency)
    return amount['value'] if amount and amount['unit']=='mg' else None


def plain_numbers(text):
    """Drop thousands separators: "1,000 mg" is 1000 mg, not 0 mg."""
    return re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', str(text or ''))


def daily_amount(text, frequency=None):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(mg|mcg|g|mEq|mL|units?)\b', plain_numbers(text), re.I)
    if not match:
        return None
    source=match.group(2).lower()
    value = float(match.group(1)) * {'mg':1, 'mcg':0.001, 'g':1000}.get(source,1)
    unit='mg' if source in ('mg','mcg','g') else {'meq':'mEq','ml':'mL','unit':'units','units':'units'}[source]
    freq = normalize(frequency or text)
    multiplier = frequency_per_day(freq)
    return {'value':value * multiplier,'unit':unit} if multiplier is not None else None


#: "three times weekly", "3x per week", "twice a week": the count per week.
PER_WEEK=re.compile(r'\b(once|twice|three times|four times|\d+ times?|\d+ ?x)\s*(?:a |per |each )?week(?:ly)?\b')


def frequency_phrase(text):
    """The first frequency phrase in free text that ``frequency_per_day`` reads."""
    text=normalize(text)
    for pattern in (PER_WEEK.pattern,r'\b(?:every|q)\s*\d+\s*(?:hours?|h)\b',
                    r'\b(?:twice|three times|four times|once) (?:daily|a day)\b',
                    r'\b(?:every other day|qod|tiw|biw|bid|tid|qid|qhs|qam|qpm|qd|nightly|at bedtime|every night|'
                    r'each morning|every morning|every evening|each evening|once a day|each day|daily|weekly|hourly)\b'):
        match=re.search(pattern,text)
        if match and frequency_per_day(match.group(0)) is not None: return match.group(0)
    return None


def frequency_per_day(text):
    """Unknown timing is unknown, never silently once daily."""
    freq=normalize(text)
    if re.search(r'\b(?:every hour|hourly|q1h)\b',freq): return 24
    interval=re.search(r'\b(?:every|q)\s*(\d+)\s*(?:hours?|h)\b',freq)
    if interval: return 24/int(interval.group(1)) if int(interval.group(1))>0 else None
    if re.search(r'\b(?:every other day|qod)\b',freq): return .5
    # "three times weekly" is common renal dosing (ESA, IV iron); read the count
    # before the bare "weekly" below claims it as once a week.
    per_week=PER_WEEK.search(freq)
    if per_week:
        word=per_week.group(1)
        return ({'once':1,'twice':2,'three times':3,'four times':4}.get(word) or int(re.match(r'\d+',word).group()))/7
    if re.search(r'\btiw\b',freq): return 3/7
    if re.search(r'\bbiw\b',freq): return 2/7
    if re.search(r'\b(?:weekly|once a week|once weekly)\b',freq): return 1/7
    if re.search(r'\bbid\b|twice|2 times',freq): return 2
    if re.search(r'\btid\b|three times|3 times',freq): return 3
    if re.search(r'\bqid\b|four times|4 times',freq): return 4
    if re.search(r'\b(?:daily|qd|qhs|nightly|at bedtime|every night|each morning|every morning|every evening|each evening|qam|qpm|once a day|each day)\b',freq): return 1
    return None


def same_titration_step(name,expected,actual,unit='mg'):
    """Optional physician-approved equivalence classes; no invented dose bands.

    Each ladder names a drug, unit, source, and steps (lists of equivalent daily
    amounts). The ordinary ±25% rule applies while this table awaits authorship.
    """
    table=json.loads(Path(__file__).with_name('titration_ladders.json').read_text())
    approvals=table.get('approval_count',len(set(table.get('approved_by',[]))))
    if table.get('status')!='approved' or approvals<2: return False
    canonical=drug(name)['name']
    for ladder in table.get('ladders',[]):
        if drug(ladder.get('drug',''))['name']!=canonical or ladder.get('unit')!=unit or not ladder.get('source'): continue
        for step in ladder.get('steps',[]):
            if expected in step and actual in step: return True
    return False
