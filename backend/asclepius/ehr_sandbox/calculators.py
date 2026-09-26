"""Offline adult calculators, explicitly unit-labelled; never a prescribing service.

Sources: NIDDK adult eGFR equations (2021 race-free); Cockcroft/Gault,
Nephron 1976 doi:10.1159/000180580; Tangri four-variable North American KFRE,
formulas reproduced in https://pmc.ncbi.nlm.nih.gov/articles/PMC7395750/ Table 1.
KFRE is restricted to adults with G3–G5 CKD and returns probability, not percent.
"""
import math

INPUTS={
 'egfr_ckd_epi_2021_cr': {'creatinine_mg_dl','age','sex'},
 'egfr_ckd_epi_2021_cr_cys': {'creatinine_mg_dl','cystatin_c_mg_l','age','sex'},
 'crcl_cockcroft_gault': {'creatinine_mg_dl','age','sex','weight_kg'},
 'kfre_4var_2yr': {'egfr','uacr_mg_g','age','sex'},
 'kfre_4var_5yr': {'egfr','uacr_mg_g','age','sex'},
 'uacr_category': {'uacr_mg_g'}, 'ckd_stage': {'egfr'}, 'bmi': {'weight_kg','height_cm'},
 'unit_convert': {'value','from_unit','to_unit','analyte'},
}


def calculate(formula,inputs):
    if formula not in INPUTS: raise ValueError('unknown formula')
    if not isinstance(inputs,dict) or set(inputs)!=INPUTS[formula]:
        raise ValueError('required inputs: '+', '.join(sorted(INPUTS[formula])))
    for key,value in inputs.items():
        if key not in ('sex','from_unit','to_unit','analyte'):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                raise ValueError(key+' must be a finite nonnegative number')
            if key in ('creatinine_mg_dl','cystatin_c_mg_l','height_cm','weight_kg','uacr_mg_g') and value==0 and formula!='uacr_category':
                raise ValueError(key+' must be positive')
    if 'age' in inputs and not 18<=inputs['age']<=120: raise ValueError('adult age must be 18 through 120')
    if 'sex' in inputs and inputs['sex'] not in ('male','female'): raise ValueError('equation requires sex male or female')
    for key in ('from_unit','to_unit','analyte'):
        if key in inputs and (not isinstance(inputs[key],str) or not inputs[key].strip()):
            raise ValueError(key+' must be a nonempty string')
    female=inputs.get('sex')=='female'; units='mL/min/1.73m2'
    if formula.startswith('egfr_'):
        ratio=inputs['creatinine_mg_dl']/(0.7 if female else 0.9); age=inputs['age']
        if formula.endswith('_cr_cys'):
            cys=inputs['cystatin_c_mg_l']/0.8
            result=135*min(ratio,1)**(-0.219 if female else -0.144)*max(ratio,1)**-0.544*min(cys,1)**-0.323*max(cys,1)**-0.778*0.9961**age*(0.963 if female else 1)
        else:
            result=142*min(ratio,1)**(-0.241 if female else -0.302)*max(ratio,1)**-1.2*0.9938**age*(1.012 if female else 1)
    elif formula=='crcl_cockcroft_gault':
        result=(140-inputs['age'])*inputs['weight_kg']/(72*inputs['creatinine_mg_dl'])*(0.85 if female else 1); units='mL/min'
    elif formula.startswith('kfre_'):
        if not 0<inputs['egfr']<60: raise ValueError('KFRE requires G3–G5 CKD: eGFR > 0 and < 60')
        lp=-0.2201*(inputs['age']/10-7.036)+0.2467*((0 if female else 1)-0.5642)-0.5567*(inputs['egfr']/5-7.222)+0.4510*(math.log(inputs['uacr_mg_g'])-5.137)
        baseline=0.975 if formula.endswith('2yr') else 0.924
        result=-math.expm1(math.log(baseline)*math.exp(lp)); units='probability (North America)'
    elif formula=='uacr_category':
        result='A1' if inputs['uacr_mg_g']<30 else 'A2' if inputs['uacr_mg_g']<=300 else 'A3'; units='category'
    elif formula=='ckd_stage':
        g=inputs['egfr']; result=next(stage for floor,stage in ((90,'G1'),(60,'G2'),(45,'G3a'),(30,'G3b'),(15,'G4'),(0,'G5')) if g>=floor); units='G category'
    elif formula=='bmi':
        result=inputs['weight_kg']/(inputs['height_cm']/100)**2; units='kg/m2'
    else:
        conversions={'creatinine':('mg/dL','umol/L',88.4),'uacr':('mg/g','mg/mmol',0.11312),'glucose':('mg/dL','mmol/L',1/18.0182)}
        if inputs['analyte'].lower() not in conversions: raise ValueError('unsupported analyte')
        left,right,factor=conversions[inputs['analyte'].lower()]
        source=inputs['from_unit'].replace('µ','u').replace('μ','u'); dest=inputs['to_unit'].replace('µ','u').replace('μ','u')
        if (source,dest)==(left,right): result=inputs['value']*factor
        elif (source,dest)==(right,left): result=inputs['value']/factor
        elif source==dest and source in (left,right): result=inputs['value']
        else: raise ValueError('unsupported units for analyte')
        units=inputs['to_unit']
    if isinstance(result,(int,float)) and not math.isfinite(result): raise ValueError('result is outside the supported numeric range')
    return {'formula':formula,'inputs':inputs,'value':round(result,6) if isinstance(result,(int,float)) else result,'unit':units}
