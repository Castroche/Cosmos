"""Isolated, network-free learning UI fixtures on port 8017; never uses operator data."""
import asyncio
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
import keyring
import uvicorn

keyring.get_password = lambda *_: None
keyring.set_password = lambda *_: None
keyring.delete_password = lambda *_: None
from cosmos_quant.api import create_app
from cosmos_quant.application_services import ApplicationServices
from cosmos_quant.settings import Settings
from cosmos_quant.runtime import NetworkFreeRuntime
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.trading_intelligence import DecisionEpisode, DeltaContext
from cosmos_quant.trading_learning import refresh_learning

context = ApplicationServices(Settings(cosmos_data_dir=Path(tempfile.mkdtemp(prefix='cosmos-ui-'))))
candles = demo_candles()
context.runtime = NetworkFreeRuntime(context.configuration_service, candles, snapshot_from_candles(candles))
for i in range(3):
    context.trading_intelligence_store.save_episode(DecisionEpisode(
        episode_id=f'fixture-evidence-{i}', symbol='BTC/USDT:USDT', status='COMPLETED',
        market_state_hash='fixture', delta=DeltaContext(current_state_hash='fixture'),
        outcome={'action':'LONG','evidence_complete':True,'trading_mode':'OKX_DEMO',
                 'realized_pnl':str(10-i*10),'fees':'1','return_on_risk':str(1-i),
                 'mfe':'2','mae':'-1','regime':'TREND_UP','setup':'pullback'}))
refresh_learning(context.trading_intelligence_store)
context.store.learning.reserve_shadow('fixture-comparison', {
    'symbol':'BTC/USDT:USDT','mode':'OKX_DEMO','primary':{'action':'LONG'},
    'control':{'action':'WAIT'},'format_valid':True,'learning_version':'fixture-version',
    'case_ids':['fixture-evidence-0'],'primary_latency_ms':1200,'control_latency_ms':1100,
    'primary_usage':{'total_tokens':300},'control_usage':[{'total_tokens':250}],
    'proxy':{'status':'COMPLETE','primary':{'status':'COMPLETE','direction_return':.01,'mfe':.02,'mae':-.01},
             'control':{'status':'NOT_DIRECTIONAL'}}})
context.store.learning.update_shadow('fixture-comparison','COMPLETED')
app = create_app(services=context)
@asynccontextmanager
async def offline_lifespan(app):
    await context.runtime.initialize()
    yield
    await context.runtime.close()
app.router.lifespan_context = offline_lifespan
uvicorn.run(app, host='127.0.0.1', port=8017, log_level='warning')
