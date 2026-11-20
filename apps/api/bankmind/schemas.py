from typing import Annotated, Literal
from datetime import date
from pydantic import BaseModel, Field, ConfigDict, model_validator

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Login(Input):
    username:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=1,max_length=200)

class WechatCode(Input):
    code:str=Field(min_length=5,max_length=256)

class TaskInput(Input):
    name:str=Field(min_length=2,max_length=120)
    account_id:str
    currency:str=Field(pattern='^[A-Z]{3}$')
    start_date:date
    end_date:date
    @model_validator(mode='after')
    def period(self):
        if self.start_date>self.end_date: raise ValueError('开始日期不能晚于结束日期')
        if (self.end_date-self.start_date).days>366: raise ValueError('单任务期间不能超过一年')
        return self

StatementMoney = str
STATEMENT_MONEY_PATTERN = r'^-?(?:0|[1-9][0-9]{0,16})(?:\.[0-9]{1,3})?$'

class StatementPage(Input):
    page:int=Field(ge=1,le=200,strict=True)
    transaction_count:int=Field(ge=0,le=20000,strict=True)
    receipts:StatementMoney=Field(pattern=STATEMENT_MONEY_PATTERN)
    payments:StatementMoney=Field(pattern=STATEMENT_MONEY_PATTERN)
    references:list[Annotated[str,Field(max_length=160)]]|None=Field(default=None,max_length=1000)

class StatementControls(Input):
    transaction_count:int=Field(ge=1,le=20000,strict=True)
    receipts:StatementMoney=Field(pattern=STATEMENT_MONEY_PATTERN)
    payments:StatementMoney=Field(pattern=STATEMENT_MONEY_PATTERN)
    opening_balance:StatementMoney=Field(pattern=STATEMENT_MONEY_PATTERN)
    closing_balance:StatementMoney=Field(pattern=STATEMENT_MONEY_PATTERN)
    source_note:str=Field(min_length=4,max_length=300)
    pages:list[StatementPage]=Field(default_factory=list,max_length=200)
    source_has_no_reference:bool=False
    balance_order:Literal['oldest_first','newest_first']|None=None

class Mapping(Input):
    fields:dict[str,str]
    confirmed:bool=False
    direction:Literal['signed_in','signed_out','debit_in','credit_in']
    date_format:Literal['ISO','DMY','MDY']
    default_currency:str=''
    default_account:str=''
    source_verified:bool=False
    statement_controls:StatementControls|None=None

class ImportSelection(Input):
    batch_id:str
    mapping:Mapping

class ImportConfirm(Input):
    selections:list[ImportSelection]=Field(min_length=1,max_length=2)

class RunInput(Input):
    single_side_declaration:Literal['UNSUBMITTED','CONFIRMED_ZERO']='UNSUBMITTED'
    reason:str=Field(default='',max_length=1000)
    quick_version:int|None=Field(default=None,ge=1)

class JobInput(Input):
    idempotency_key:str=Field(min_length=8,max_length=100)

class PreviewJobInput(JobInput):
    kind:Literal['PREVIEW']
    side:Literal['BANK','BOOK']
    filename:str=Field(min_length=1,max_length=200)
    size:int=Field(ge=1,le=10*1024*1024)
    sha256:str=Field(pattern='^[0-9a-f]{64}$')
    sheet:str=Field(default='',max_length=120)
    header_row:int=Field(default=1,ge=1,le=20)

class PublishJobInput(JobInput, ImportConfirm):
    kind:Literal['PUBLISH']
    version:int=Field(ge=1)

class MatchJobInput(JobInput):
    kind:Literal['MATCH']
    version:int=Field(ge=1)
    rerun:bool=False
    single_side_declaration:Literal['UNSUBMITTED','CONFIRMED_ZERO']='UNSUBMITTED'
    reason:str=Field(default='',max_length=1000)
    require_both:bool=False

class Rules(Input):
    version:int
    date_days:int=Field(ge=0,le=10)
    amount_absolute:str=Field(pattern=r'^\d{1,8}(\.\d{1,2})?$')
    amount_relative:str=Field(pattern=r'^0(\.\d{1,6})?$')
    reason:str=Field(min_length=4,max_length=1000)

class Decision(Input):
    version:int=Field(ge=1)
    action:Literal['APPROVE','REJECT','RETURN','REQUEST_EVIDENCE']
    reason:str=Field(min_length=4,max_length=2000)
    idempotency_key:str=Field(min_length=8,max_length=100)

class Proposal(Input):
    bank_ids:list[str]=Field(min_length=1,max_length=3)
    book_ids:list[str]=Field(min_length=1,max_length=3)
    reason:str=Field(min_length=4,max_length=2000)

class CaseUpdate(Input):
    version:int
    action:Literal['NOTE','ASSIGN','INVESTIGATE','SUBMIT','REOPEN']
    reason:str=Field(min_length=4,max_length=2000)
    owner_id:str=''
    due_date:str=''
    resolution:Literal['','BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT','DATA_ERROR','OTHER']=''

class QualityAction(Input):
    action:Literal['VALID','EXCLUDED']
    reason:str=Field(min_length=8,max_length=2000)

class QualityBulk(Input):
    ids:list[str]=Field(min_length=1,max_length=50)
    reference:str=Field(min_length=1,max_length=160)
    side:Literal['BANK','BOOK']
    action:Literal['VALID','EXCLUDED']
    reason:str=Field(min_length=8,max_length=2000)
    idempotency_key:str=Field(min_length=8,max_length=100)

class Balances(Input):
    version:int
    BANK:dict[str,str | None]
    BOOK:dict[str,str | None]
    reason:str=Field(min_length=4,max_length=1000)

class PeriodRequest(Input):
    version:int=Field(ge=1)
    reason:str=Field(min_length=8,max_length=2000)

class UserInput(Input):
    username:str=Field(min_length=3,max_length=80)
    name:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=10,max_length=200)
    role:Literal['Admin','Accountant','Reviewer','Auditor']
    account_ids:list[str]

class UserUpdate(Input):
    enabled:bool
    account_ids:list[str]
    reason:str=Field(min_length=4,max_length=1000)

class AccountInput(Input):
    name:str=Field(min_length=2,max_length=100)
    number:str=Field(pattern=r'^\d{8,34}$')
    bank:str=Field(min_length=2,max_length=80)
    currency:Literal['CNY','USD','EUR','HKD','JPY','KWD']='CNY'
