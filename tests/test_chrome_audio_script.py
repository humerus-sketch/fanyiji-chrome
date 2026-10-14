"""Execute the real bridge/worklet in a deterministic render-thread harness."""
import base64
import json
import shutil
import subprocess

import pytest

from fanyiji.chrome_audio_script import (FRAME_SAMPLES, _WORKLET, install_script,
                                        pull_script, push_script, status_script, stop_script)

NONCE = "a" * 32
URL = "https://live.bilibili.com/1698225"
NODE = shutil.which("node")

HARNESS = r"""
globalThis.window=globalThis;
globalThis.location={origin:'https://live.bilibili.com',pathname:'/1698225'};
globalThis.currentTime=0;
Date.now=()=>1700000000000+currentTime*1000;
const timers=new Map(); let timerSerial=0;
globalThis.setInterval=fn=>{timers.set(++timerSerial,fn);return timerSerial;};
globalThis.clearInterval=id=>timers.delete(id);
Object.defineProperty(globalThis,'performance',{value:{now:()=>currentTime*1000}});
const video={isConnected:true,paused:false,ended:false,readyState:4,muted:false,volume:1};
let videos=[video];
globalThis.document={querySelectorAll:()=>videos};
const trace=[];const nodes=[];const contexts=[];
let Processor;
globalThis.AudioWorkletProcessor=class {
  constructor(){this.port={onmessage:null,postMessage:()=>{}};}
};
globalThis.registerProcessor=(name,ctor)=>{Processor=ctor;};
globalThis.AudioWorkletNode=class {
  constructor(context,name,options){
    this.processor=new Processor(options);nodes.push(this);
    this.port={onmessage:null,postMessage:(value,transfer=[])=>
      this.processor.port.onmessage({data:structuredClone(value,{transfer})})};
    this.processor.port.postMessage=(value,transfer=[])=>{
      const copied=structuredClone(value,{transfer});if(this.port.onmessage)this.port.onmessage({data:copied});};
  }
  connect(dest){trace.push(['processed_output',dest]);}
};
globalThis.AudioContext=class {
  constructor(){this.sampleRate=16000;this.state='suspended';this.destination={name:'destination'};
    this.audioWorklet={addModule:async()=>{}};contexts.push(this);}
  async resume(){this.state='running';}
  async close(){this.state='closed';}
  get currentTime(){return globalThis.currentTime;}
  createMediaElementSource(selected){trace.push(['source',selected===video]);
    return {connect:node=>trace.push(['source_output',node===this.destination?'direct':'worklet']),disconnect:()=>{}};}
};
function assert(value,message='assertion failed'){if(!value)throw Error(message);}
function render(value=.75,count=1){
  let output;
  for(let index=0;index<count;index++){
    const input=[new Float32Array(128).fill(value),new Float32Array(128).fill(-value/2)];
    output=[new Float32Array(128),new Float32Array(128)];
    nodes[nodes.length-1].processor.process([input],[output]);currentTime+=128/16000;
  }
  return output;
}
function pcm(value){const bytes=Buffer.alloc(6400);for(let i=0;i<3200;i++)bytes.writeInt16LE(Math.round(value*32768),i*2);return bytes.toString('base64');}
"""


def run_js(body):
    if not NODE:
        pytest.skip("Node.js is needed only for deterministic JavaScript tests")
    script = HARNESS + "\n" + _WORKLET + "\n(async()=>{\n" + body + "\nconsole.log('ok');})().catch(error=>{console.error(error.stack);process.exitCode=1;});"
    result = subprocess.run([NODE], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"


def expression(value):
    return "(" + value + ")"


def install():
    return "await " + expression(install_script(NONCE, 1, URL))


def pull():
    return expression(pull_script(NONCE, 1))


def test_graph_has_only_one_output_and_capture_precedes_playback():
    run_js(f"""
    const initial={install()}; assert(initial.state==='buffering');
    assert(trace.filter(row=>row[0]==='source').length===1);
    assert(trace.filter(row=>row[0]==='source_output').every(row=>row[1]==='worklet'));
    assert(trace.filter(row=>row[0]==='processed_output').length===1);
    const startup=render(.8,50);assert(startup.every(channel=>channel.every(sample=>sample===0)));
    const first={pull()};assert(first.frames.length===2);assert(first.frames[0].sequence===0);
    const bytes=Buffer.from(first.frames[0].pcm_s16le,'base64');assert(bytes.length===6400);
    assert(Math.abs(bytes.readInt16LE(0)/32768-.2)<.0001);
    const active=window.__fanyijiChromeAudioV1.active;
    const accepted=active.push({{frames:first.frames.map(frame=>({{...frame,pcm_s16le:pcm(.1)}}))}});
    assert(accepted.accepted_frames===2);
    const wet=render(.8,50);assert(wet.every(channel=>channel.every(sample=>Math.abs(sample-.1)<.0001)));
    const second={pull()};assert(second.frames[0].sequence===2);
    const raw=Buffer.from(second.frames[0].pcm_s16le,'base64');
    assert(Math.abs(raw.readInt16LE(0)/32768-.2)<.0001,'processed output entered capture feedback');
    assert(active.status().processed_only_verified===false);
    """)


@pytest.mark.parametrize("silent", ["{...video,muted:true}","{...video,volume:0}"])
def test_silent_auxiliary_player_does_not_mask_the_audible_live_player(silent):
    run_js(f"""
    videos=[video,{silent}];const result={install()};assert(result.state==='buffering');
    assert(trace.filter(row=>row[0]==='source').length===1);
    assert(trace.find(row=>row[0]==='source')[1]===true);
    render(.8,25);assert({pull()}.frames.length===1);
    """)


def test_stop_preserves_dry_stereo_and_rejects_old_pcm():
    run_js(f"""
    {install()};render(.8,25);const captured={pull()};
    const stopped={expression(stop_script(NONCE,1))};assert(stopped.state==='stopped');
    assert(stopped.reason==='disabled');assert(timers.size===0);
    const dry=render(.8);assert(Math.abs(dry[0][0]-.8)<.0001);assert(Math.abs(dry[1][0]+.4)<.0001);
    const late=window.__fanyijiChromeAudioV1.active.push({{frames:captured.frames}});
    assert(late.accepted_frames===0);assert(contexts[0].state==='running');
    const after={pull()};assert(after.state==='stopped');assert(after.failure_reason==='');
    assert(after.frames.length===0);assert(after.reason==='disabled');
    """)


def test_session_switch_reuses_source_without_accepting_prior_results():
    new_nonce = "b" * 32
    run_js(f"""
    {install()};render(.8,25);const prior={pull()};
    const next=await {expression(install_script(new_nonce,2,URL))};assert(next.state==='buffering');
    assert(trace.filter(row=>row[0]==='source').length===1,'second MediaElementSource attachment');
    assert({expression(pull_script(NONCE,1))}.reason==='stale_session');
    const active=window.__fanyijiChromeAudioV1.active;
    active.graph.node.port.onmessage({{data:{{kind:'input',session_nonce:{json.dumps(NONCE)},generation:1,
      sequence:0,start_sample:0,samples:new Float32Array(3200)}}}});
    assert(active.queue.length===0);render(.8,25);
    const fresh={expression(pull_script(new_nonce,2))};assert(fresh.frames[0].sequence===0);
    """)


def test_output_sequence_and_atomic_batch_validation():
    run_js(f"""
    {install()};render(.8,50);const captured={pull()};const active=window.__fanyijiChromeAudioV1.active;
    let response=active.push({{frames:[captured.frames[1]]}});
    assert(response.error==='invalid_output');assert(active.offered.size===2);
    response=active.push({{frames:[captured.frames[0],{{...captured.frames[1],pcm_s16le:'bad'}}]}});
    assert(response.error==='invalid_output');assert(nodes[0].processor.received===0);
    response=active.push({{frames:captured.frames}});assert(response.accepted_frames===2);
    response=active.push({{frames:captured.frames}});assert(response.error==='invalid_output');
    assert(nodes[0].processor.received===2);
    """)


def test_underflow_latches_dry_bypass_instead_of_replaying_old_frames():
    run_js(f"""
    {install()};render(.8,50);const captured={pull()};
    const active=window.__fanyijiChromeAudioV1.active;active.push({{frames:captured.frames}});
    render(.8,50);const output=render(.8);
    assert(nodes[0].processor.mode==='bypassed');assert(active.reason==='playback_underrun');
    assert(Math.abs(output[0][0]-.8)<.0001);assert(active.queue.length===0);assert(active.offered.size===0);
    """)


def test_first_render_fault_survives_cleanup_late_status_and_session_restart():
    next_nonce = "b" * 32
    run_js(f"""
    {install()};render(.8,50);const captured={pull()};
    const active=window.__fanyijiChromeAudioV1.active;active.push({{frames:captured.frames}});
    render(.8,50);render(.8);
    const failed=active.status();assert(failed.failure_reason==='playback_underrun');
    assert(failed.failure_state==='processing');assert(Number.isFinite(failed.first_failure_wall_ms));
    const stopped={expression(stop_script(NONCE,1))};
    assert(stopped.state==='stopped' && stopped.reason==='disabled');
    assert(stopped.failure_reason==='playback_underrun');
    assert(stopped.first_failure_wall_ms===failed.first_failure_wall_ms);
    active.graph.node.port.onmessage({{data:{{kind:'status',session_nonce:{json.dumps(NONCE)},generation:1,
      state:'processing',reason:'processed_playback',processed_frames:999,played_frames:999}}}});
    assert(active.status().state==='stopped' && active.status().processed_frames!==999);
    const fresh=await {expression(install_script(next_nonce,2,URL))};
    assert(fresh.failure_reason==='' && fresh.failure_state==='');assert(fresh.first_failure_wall_ms===null);
    assert(fresh.first_input_wall_ms===null && fresh.first_playback_wall_ms===null);
    assert(nodes[0].processor.failureReason==='');assert(nodes[0].processor.failureTime===null);
    assert(trace.filter(row=>row[0]==='source').length===1);
    const stale={expression(stop_script(NONCE,1,reason='playback_underrun'))};
    assert(stale.reason==='stale_session');assert(window.__fanyijiChromeAudioV1.active.status().state==='buffering');
    """)


def test_main_thread_fault_cannot_be_cleared_by_delayed_processing_report():
    run_js(f"""
    {install()};const active=window.__fanyijiChromeAudioV1.active;
    active.bypass('capture_overflow');
    active.graph.node.port.onmessage({{data:{{kind:'status',session_nonce:{json.dumps(NONCE)},generation:1,
      state:'processing',reason:'processed_playback',processed_frames:0,played_frames:0,
      failure_reason:'',failure_state:'',buffered_output_frames:2}}}});
    assert(active.mode==='bypassed');assert(active.reason==='capture_overflow');
    assert(active.status().failure_reason==='capture_overflow');
    const stopped={expression(stop_script(NONCE,1,reason='controller_timeout'))};
    assert(stopped.failure_reason==='capture_overflow');assert(stopped.failure_state==='buffering');
    """)


def test_pcm_telemetry_separates_input_output_submission_and_actual_playback():
    run_js(f"""
    const initial={install()};assert(initial.first_input_wall_ms===null);
    assert(initial.last_input_age_ms===null && initial.last_output_age_ms===null);
    render(.8,50);const frames={pull()}.frames;const active=window.__fanyijiChromeAudioV1.active;
    const input=active.status();assert(input.captured_frames===2);
    assert(Math.abs(input.last_input_rms-.2)<.0001);
    assert(Math.abs(input.first_input_wall_ms-1700000000000)<.1);
    assert(input.last_input_age_ms>=0 && input.first_playback_wall_ms===null);
    active.push({{frames:frames.map(frame=>({{...frame,pcm_s16le:pcm(.1)}}))}});
    const submitted=active.status();assert(submitted.last_output_wall_ms!==null);
    assert(submitted.last_output_age_ms===0);assert(submitted.first_playback_wall_ms===null);
    assert(Math.abs(submitted.last_output_rms-.1)<.0001,'RMS measured after detached transfer');
    render(.8,30);const playing=active.status();
    assert(playing.played_frames>=1 && playing.first_playback_wall_ms!==null);
    assert(Math.abs(playing.first_playback_wall_ms-1700000000400)<.1);
    assert(playing.last_playback_wall_ms>=playing.first_playback_wall_ms);
    assert(playing.last_output_age_ms>0 && playing.last_input_age_ms>=0);
    assert(Number.isFinite(playing.audio_context_time_seconds));
    for(const key of ['failure_reason','failure_state','first_failure_wall_ms',
      'first_input_wall_ms','last_input_wall_ms','last_output_wall_ms','first_playback_wall_ms',
      'last_playback_wall_ms','last_input_age_ms','last_output_age_ms','last_input_rms','last_output_rms'])
      assert(key in playing,key);
    """)


def test_mono_pcm_keeps_sample_order_and_frame_clock():
    run_js(f"""
    {install()};const processor=nodes[0].processor;
    for(let block=0;block<50;block++){{
      const mono=new Float32Array(128);
      for(let index=0;index<128;index++)mono[index]=(block*128+index)/16000-.25;
      processor.process([[mono]],[[new Float32Array(128),new Float32Array(128)]]);
      currentTime+=128/16000;
    }}
    const captured={pull()};assert(captured.frames.length===2);
    for(let frame=0;frame<2;frame++){{
      const value=captured.frames[frame], bytes=Buffer.from(value.pcm_s16le,'base64');
      assert(value.sequence===frame && value.start_sample===frame*3200);
      for(let index=0;index<3200;index+=317){{
        const expected=(frame*3200+index)/16000-.25;
        assert(Math.abs(bytes.readInt16LE(index*2)/32768-expected)<.00004,'PCM reordered');
      }}
    }}
    """)


@pytest.mark.parametrize("reason", ["disabled", "playback_underrun", "invalid_output", "source_changed",
    "processing_error", "processing_too_slow", "reset_error", "load_error",
    "audio_output_rejected", "audio_stream_unavailable", "script_timeout", "script_unavailable",
    "browser_unavailable"])
def test_cleanup_can_report_validated_runtime_fault(reason):
    run_js(f"""
    {install()};const result={expression(stop_script(NONCE,1,reason=reason))};
    assert(result.state==='stopped' && result.reason==={json.dumps(reason)});
    assert(result.failure_reason==={json.dumps('' if reason == 'disabled' else reason)});
    """)


@pytest.mark.parametrize("reason", ["unknown_secret", "disabled';alert(1)", None, 42])
def test_cleanup_rejects_arbitrary_runtime_reason(reason):
    with pytest.raises(ValueError, match="invalid_audio_stop_reason"):
        stop_script(NONCE, 1, reason=reason)


def test_startup_timeout_and_disconnected_controller_restore_sound():
    run_js(f"""
    {install()};const output=render(.8,255);
    assert(nodes[0].processor.mode==='bypassed');assert(Math.abs(output[0][0]-.8)<.0001);
    assert(window.__fanyijiChromeAudioV1.active.reason==='startup_timeout');
    assert(window.__fanyijiChromeAudioV1.active.status().failure_reason==='startup_timeout');
    for(const callback of Array.from(timers.values()))callback();
    assert(window.__fanyijiChromeAudioV1.active.reason==='controller_timeout');assert(timers.size===0);
    assert(window.__fanyijiChromeAudioV1.active.status().failure_reason==='startup_timeout');
    """)


def test_capture_and_inflight_combined_limit_fail_open():
    run_js(f"""
    {install()};const active=window.__fanyijiChromeAudioV1.active;
    for(let sequence=0;sequence<11;sequence++){{
      active.graph.node.port.onmessage({{data:{{kind:'input',session_nonce:{json.dumps(NONCE)},generation:1,
        sequence,start_sample:sequence*3200,samples:new Float32Array(3200)}}}});
      if(sequence===4){pull()};
    }}
    assert(active.reason==='capture_overflow');assert(active.dropped===1);
    assert(active.queue.length===0);assert(active.offered.size===0);
    """)


def test_source_navigation_flushes_audio_and_never_captures_new_page():
    run_js(f"""
    {install()};render(.8,25);location.pathname='/21399689';
    const result={pull()};assert(result.reason==='source_changed');assert(result.frames.length===0);
    for(const callback of Array.from(timers.values()))callback();
    assert(window.__fanyijiChromeAudioV1.active.stopped);assert(timers.size===0);
    """)


@pytest.mark.parametrize("mutation,reason", [
    ("videos=[]", "media_not_playing"),
    ("videos=[video,{...video}]", "ambiguous_media"),
    ("location.pathname='/21399689'", "wrong_source"),
    ("globalThis.AudioWorkletNode=undefined", "audio_worklet_unavailable"),
    ("video.muted=true", "media_not_playing"),
    ("video.volume=0", "media_not_playing"),
])
def test_invalid_capture_preflight_never_attaches_media(mutation, reason):
    run_js(f"{mutation};const result={install()};assert(result.reason==={json.dumps(reason)});assert(trace.length===0);")


def test_module_failure_before_attachment_preserves_original_player():
    run_js(f"""
    const Base=AudioContext;
    globalThis.AudioContext=class extends Base{{constructor(){{super();this.audioWorklet.addModule=async()=>{{throw Error('CSP');}};}}}};
    const result={install()};assert(result.reason==='audio_graph_unavailable');assert(trace.length===0);
    assert(contexts[0].state==='closed');
    """)


def test_async_navigation_during_module_load_does_not_attach_new_source():
    run_js(f"""
    const Base=AudioContext;
    globalThis.AudioContext=class extends Base{{constructor(){{super();this.audioWorklet.addModule=async()=>{{location.pathname='/21399689';}};}}}};
    const result={install()};assert(result.reason==='audio_graph_unavailable');assert(trace.length===0);
    """)


@pytest.mark.parametrize("nonce,generation", [("tiny",1),("a;alert(1)",1),(NONCE,True),(NONCE,-1),(NONCE,2**31)])
def test_invalid_session_identity_rejected(nonce,generation):
    with pytest.raises(ValueError):
        install_script(nonce,generation,URL)


@pytest.mark.parametrize("url", ["http://live.bilibili.com/1698225","https://example.com/1698225", "file:///C:/test.html"])
def test_install_restricted_to_explicit_live_source(url):
    with pytest.raises(ValueError):
        install_script(NONCE,1,url)


def test_script_builder_checks_exact_pcm_shape_order_and_payload():
    pcm=base64.b64encode(b"\x00"*(FRAME_SAMPLES*2)).decode()
    frame={"sequence":0,"start_sample":0,"pcm_s16le":pcm}
    assert "accepted_frames" in push_script(NONCE,1,[frame])
    for invalid in ({**frame,"sequence":True},{**frame,"start_sample":1},{**frame,"pcm_s16le":"x"},
                    {**frame,"pcm_s16le":"?"*len(pcm)}):
        with pytest.raises(ValueError):push_script(NONCE,1,[invalid])
    with pytest.raises(ValueError):push_script(NONCE,1,[frame,frame])
    with pytest.raises(ValueError):pull_script(NONCE,1,max_frames=9)
    with pytest.raises(ValueError):install_script(NONCE,1,URL,frame_samples=128)
    with pytest.raises(ValueError):install_script(NONCE,1,URL,prebuffer_frames=6)


def test_generated_commands_parse_and_never_issue_website_requests():
    scripts=[install_script(NONCE,1,URL),pull_script(NONCE,1),status_script(NONCE,1),stop_script(NONCE,1)]
    run_js("\n".join("new Function('return '+"+json.dumps(script)+");" for script in scripts))
    for script in scripts:
        assert "fetch(" not in script and "XMLHttpRequest" not in script
        assert "getUserMedia" not in script and "createMediaStreamSource" not in script
