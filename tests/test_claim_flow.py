"""watch_loop on synthetic pages that mimic the chat-footer drop widget."""
import asyncio

import main as m
from helpers import CAMPAIGN, drain_logs, make_worker, open_page
from playwright.async_api import async_playwright

BASE = """<html><body><div class="TopMenuRightAuthorized_x">me</div>
<vk-video-player><div class="state-container" data-playback-state="%STATE%"></div></vk-video-player>
<div class="ChatFooter_root_a"><div class="StreamChatFooter_root_b">
<div class="PointActions_root_c"><button>620</button></div>
<div class="TooltipFloating_root_d" id="slot" style="%SLOT_STYLE%"></div></div></div>
<div id="popup"></div><script>%SCRIPT%</script></body></html>"""

# Ring 0 -> 100%, becomes a box icon; click -> list with «Получить» -> «Награда получена» / «Понятно».
# After reward 1 the ring starts over; after reward 2 the widget disappears.
TWO_REWARDS = """
let reward = 1, pct = 0, mode = 'ring';
const slot = document.getElementById('slot'), popup = document.getElementById('popup');
function render(){
  if (mode === 'ring') slot.innerHTML = '<div class="CircularProgressBar_root_z"><span class="CircularProgressBar_countdownPercent_w">'+pct+'%</span></div>';
  else if (mode === 'box') slot.innerHTML = '<div class="DropBoxIcon_q" style="width:30px;height:30px" id="box">BOX</div>';
  else slot.innerHTML = '';
}
render();
setInterval(()=>{ if (mode==='ring'){ pct=Math.min(100,pct+25); if(pct>=100) mode='box'; render(); } }, 400);
document.addEventListener('click', e=>{
  const t = e.target;
  if (t.id==='box'){ popup.innerHTML='<div>Боксы</div><button id="claim">Получить</button>'; }
  if (t.id==='claim'){ popup.innerHTML='<div>Награда получена</div><button id="ok">Понятно</button>'; }
  if (t.id==='ok'){ popup.innerHTML=''; if (reward===1){ reward=2; pct=0; mode='ring'; } else { mode='none'; } render(); }
});
"""

NEED_VK_ID = """
const slot=document.getElementById('slot');
slot.innerHTML='<div class="CircularProgressBar_root_z"><span class="CircularProgressBar_countdownPercent_w">100%</span></div>';
slot.addEventListener('click',()=>{ document.getElementById('popup').innerHTML='<div>Не подключен VK ID</div><button id="ok">Понятно</button>'; });
document.addEventListener('click',e=>{ if(e.target.id==='ok') document.getElementById('popup').innerHTML=''; });
"""

ONE_RING = ("const slot=document.getElementById('slot'); slot.innerHTML="
            "'<div class=\"CircularProgressBar_root_z\"><span class=\"CircularProgressBar_countdownPercent_w\">10%</span></div>';")


async def _run(state, script, slot_style="display:inline-block", idle_timeout=None):
    html = BASE.replace("%STATE%", state).replace("%SCRIPT%", script).replace("%SLOT_STYLE%", slot_style)
    old = m.IDLE_TIMEOUT_S
    if idle_timeout is not None:
        m.IDLE_TIMEOUT_S = idle_timeout
    try:
        async with async_playwright() as pw:
            browser, ctx = await open_page(pw)
            page = await ctx.new_page()
            await page.set_content(html)
            w, q = make_worker(page)
            result = await asyncio.wait_for(w.watch_loop(CAMPAIGN), 60)
            await browser.close()
            return result, drain_logs(q)
    finally:
        m.IDLE_TIMEOUT_S = old


def test_two_rewards_then_campaign_finished():
    result, logs = asyncio.run(_run("playing", TWO_REWARDS, slot_style=""))  # slot is a wide block: click must still hit the icon
    assert result == "finished"
    assert sum("Награда получена (за сессию" in l for l in logs) == 2
    assert any("следующая награда" in l for l in logs)  # it noticed that more rewards exist and kept mining


def test_missing_vk_id_stops_the_campaign():
    result, _ = asyncio.run(_run("playing", NEED_VK_ID))
    assert result == "failed"


def test_stream_without_drop_widget_is_idle():
    result, _ = asyncio.run(_run("playing", "", idle_timeout=1))
    assert result == "idle"


def test_paused_stream_triggers_restart():
    result, _ = asyncio.run(_run("paused", ONE_RING))
    assert result == "restart"
