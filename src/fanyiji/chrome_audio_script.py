"""Same-page, bounded WebAudio bridge for an opt-in Chrome caption experiment.

The bridge captures the media element before playback, never the system output.
It cannot certify Chrome's internal ASR input; that needs a separate routing test.
Stopping restores dry WebAudio playback. A reload restores the native media sink.
"""
from __future__ import annotations

import base64
import json
import re

from .chrome_caption_runtime import validate_source_url

SAMPLE_RATE = 16000
FRAME_SAMPLES = 3200
MAX_BUFFER_FRAMES = 10
BRIDGE_KEY = "__fanyijiChromeAudioV1"
STOP_REASONS = frozenset({
    "disabled", "session_replaced", "playback_underrun", "capture_overflow",
    "controller_timeout", "startup_timeout", "invalid_output", "invalid_capture",
    "source_changed",
    "processing_error", "processing_too_slow", "reset_error", "load_error",
    "audio_output_rejected", "audio_stream_unavailable", "script_timeout",
    "script_unavailable", "browser_unavailable",
})


def _identity(session_nonce: str, generation: int) -> dict:
    if not isinstance(session_nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", session_nonce):
        raise ValueError("invalid_audio_session")
    if type(generation) is not int or not 0 <= generation <= 2147483647:
        raise ValueError("invalid_audio_generation")
    return {"session_nonce": session_nonce, "generation": generation}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


# A render-thread watchdog also works when the page's main thread is stalled.
# PCM leaves this graph solely through MessagePort and bounded CDP evaluations.
_WORKLET = r"""
class FanyijiCaptionBridge extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.configure(options.processorOptions);
    this.port.onmessage = event => {
      const value = event.data;
      if (!value || typeof value !== 'object') return;
      if (value.kind === 'configure') { this.configure(value); return; }
      if (!this.matches(value)) return;
      if (value.kind === 'bypass') { this.bypass(value.reason || 'disabled'); return; }
      if (value.kind === 'output' && this.mode !== 'bypassed') {
        if (!(value.samples instanceof Float32Array) || value.samples.length !== this.frameSamples ||
            !Number.isSafeInteger(value.sequence) || value.sequence !== this.outputSequence ||
            this.output.length >= this.maxFrames) { this.bypass('invalid_output'); return; }
        for (const item of value.samples) if (!Number.isFinite(item) || Math.abs(item) > 1) {
          this.bypass('invalid_output'); return;
        }
        this.outputSequence++;
        this.output.push(value.samples);
        this.received++;
      }
    };
  }
  matches(value) { return value.session_nonce === this.nonce && value.generation === this.generation; }
  configure(value) {
    this.nonce = value.session_nonce; this.generation = value.generation;
    this.frameSamples = value.frame_samples; this.maxFrames = value.max_frames;
    this.prebufferFrames = value.prebuffer_frames;
    this.input = new Float32Array(this.frameSamples); this.inputOffset = 0; this.inputEnergy = 0;
    this.sequence = 0; this.samplePosition = 0; this.outputSequence = 0;
    this.output = []; this.outputOffset = 0; this.received = 0; this.played = 0;
    this.mode = 'buffering'; this.reason = 'prebuffer';
    this.failureReason = ''; this.failureState = ''; this.failureTime = null;
    this.firstInputTime = null; this.firstPlaybackTime = null; this.lastPlaybackTime = null;
    this.started = currentTime; this.lastReport = currentTime;
    this.report();
  }
  report() {
    this.port.postMessage({kind: 'status', session_nonce: this.nonce, generation: this.generation,
      state: this.mode, reason: this.reason, buffered_output_frames: this.output.length,
      processed_frames: this.received, played_frames: this.played,
      failure_reason: this.failureReason, failure_state: this.failureState,
      failure_audio_time_seconds: this.failureTime,
      first_input_audio_time_seconds: this.firstInputTime,
      first_playback_audio_time_seconds: this.firstPlaybackTime,
      last_playback_audio_time_seconds: this.lastPlaybackTime,
      audio_context_time_seconds: currentTime});
  }
  bypass(reason) {
    if (this.mode === 'bypassed') return;
    if (!this.failureReason && reason !== 'disabled' && reason !== 'session_replaced') {
      this.failureReason = reason; this.failureState = this.mode; this.failureTime = currentTime;
    }
    this.mode = 'bypassed'; this.reason = reason; this.output = []; this.inputOffset = 0;
    this.report();
  }
  process(inputs, outputs) {
    const input = inputs[0] || []; const output = outputs[0] || [];
    if (!output.length) return true;
    const length = output[0].length;
    if (this.mode === 'buffering') {
      if (this.output.length >= this.prebufferFrames) {
        this.mode = 'processing'; this.reason = 'processed_playback'; this.report();
      } else if (currentTime - this.started > 2) this.bypass('startup_timeout');
    }
    for (let index = 0; index < length; index++) {
      let raw = 0;
      if (input.length) {
        for (const channel of input) {
          const item = channel[index] || 0;
          raw += Number.isFinite(item) ? Math.max(-1, Math.min(1, item)) : 0;
        }
        raw /= input.length;
      }
      if (this.mode !== 'bypassed' && input.length) {
        if (this.firstInputTime === null) this.firstInputTime = currentTime + index / 16000;
        this.input[this.inputOffset++] = raw;
        this.inputEnergy += raw * raw;
        if (this.inputOffset === this.frameSamples) {
          const samples = this.input;
          this.port.postMessage({kind: 'input', session_nonce: this.nonce, generation: this.generation,
            sequence: this.sequence++, start_sample: this.samplePosition, samples,
            rms: Math.sqrt(this.inputEnergy / this.frameSamples),
            first_input_audio_time_seconds: this.firstInputTime,
            audio_time_seconds: currentTime + (index + 1) / 16000}, [samples.buffer]);
          this.samplePosition += this.frameSamples;
          this.input = new Float32Array(this.frameSamples); this.inputOffset = 0; this.inputEnergy = 0;
        }
      }
      let sample = 0;
      if (this.mode === 'processing') {
        if (!this.output.length) this.bypass('playback_underrun');
        else {
          if (this.firstPlaybackTime === null) this.firstPlaybackTime = currentTime + index / 16000;
          this.lastPlaybackTime = currentTime + index / 16000;
          sample = this.output[0][this.outputOffset++];
          if (this.outputOffset === this.frameSamples) {
            this.output.shift(); this.outputOffset = 0; this.played++;
          }
        }
      }
      for (let channel = 0; channel < output.length; channel++) {
        // Bypass preserves stereo and never plays already-heard buffered material.
        const dry = input.length ? (input[Math.min(channel, input.length - 1)][index] || 0) : 0;
        output[channel][index] = this.mode === 'bypassed'
          ? (Number.isFinite(dry) ? Math.max(-1, Math.min(1, dry)) : 0) : sample;
      }
    }
    if (currentTime - this.lastReport >= .2) { this.lastReport = currentTime; this.report(); }
    return true;
  }
}
registerProcessor('fanyiji-caption-audio-v1', FanyijiCaptionBridge);
"""

_INSTALL = r"""
(async () => {
  const cfg = __CFG__;
  const key = '__fanyijiChromeAudioV1';
  const canonical = () => location.origin + location.pathname.replace(/\/$/, '');
  const empty = (reason) => ({state:'unavailable', reason, sample_rate:16000,
    session_nonce:cfg.session_nonce, generation:cfg.generation, processed_only_verified:false});
  if (canonical() !== cfg.url) return empty('wrong_source');
  const videos = Array.from(document.querySelectorAll('video')).filter(video =>
    video.isConnected && !video.paused && !video.ended && video.readyState >= 2 &&
    !video.muted && Number.isFinite(video.volume) && video.volume > 0);
  if (videos.length !== 1) return empty(videos.length ? 'ambiguous_media' : 'media_not_playing');
  const video = videos[0];
  let manager = window[key];
  if (manager && manager.brand !== 'fanyiji-audio-v1') return empty('bridge_conflict');
  if (!manager) {
    manager = {brand:'fanyiji-audio-v1', graphs:new WeakMap(), active:null};
    Object.defineProperty(window, key, {value:manager, configurable:false, enumerable:false});
  }
  if (manager.active && manager.active.matches(cfg) && manager.active.video === video)
    return manager.active.status();
  if (manager.active) manager.active.stop('session_replaced');
  let graph = manager.graphs.get(video);
  if (!graph) {
    if (typeof AudioContext !== 'function' || typeof AudioWorkletNode !== 'function')
      return empty('audio_worklet_unavailable');
    let context, blobUrl, source;
    try {
      context = new AudioContext({sampleRate:16000, latencyHint:'interactive'});
      if (context.sampleRate !== 16000 || !context.audioWorklet) throw Error('unsupported');
      blobUrl = URL.createObjectURL(new Blob([__WORKLET__], {type:'application/javascript'}));
      await context.audioWorklet.addModule(blobUrl);
      await context.resume();
      if (context.state !== 'running') throw Error('not_running');
      if(canonical()!==cfg.url || !video.isConnected || video.paused) throw Error('source_changed');
      const node = new AudioWorkletNode(context, 'fanyiji-caption-audio-v1', {
        numberOfInputs:1, numberOfOutputs:1, outputChannelCount:[2], processorOptions:cfg});
      // Attach only after module/context are ready: earlier failure leaves the original player intact.
      source = context.createMediaElementSource(video);
      source.connect(node); node.connect(context.destination);
      graph = {context, source, node}; manager.graphs.set(video, graph);
    } catch (_) {
      if (source && context && context.state==='running') {
        // A media-element attachment is permanent. Keep its context alive and
        // restore a direct path if constructing the processor connection fails.
        try { source.disconnect(); source.connect(context.destination); } catch (_) {}
      } else if (context) try { await context.close(); } catch (_) {}
      return empty('audio_graph_unavailable');
    } finally { if (blobUrl) URL.revokeObjectURL(blobUrl); }
  }
  const state = {
    video, graph, nonce:cfg.session_nonce, generation:cfg.generation, url:cfg.url,
    queue:[], offered:new Map(), captured:0, dropped:0, lastSequence:-1,
    mode:'buffering', reason:'prebuffer', processed:0, played:0, outputCount:0,
    stopped:false, lastPoll:performance.now(), timer:null,
    failureReason:'', failureState:'', firstFailureWall:null,
    firstInputWall:null, lastInputWall:null, lastInputAt:null, lastInputRms:0,
    lastOutputWall:null, lastOutputAt:null, lastOutputRms:0,
    firstPlaybackWall:null, lastPlaybackWall:null, audioTime:null,
    matches(value) { return value.session_nonce === this.nonce && value.generation === this.generation; },
    valid() { return !this.stopped && canonical() === this.url && this.video.isConnected; },
    audioWall(time) {
      const now = this.graph.context.currentTime;
      return Number.isFinite(time) && Number.isFinite(now)
        ? Date.now() - Math.max(0, now - time) * 1000 : Date.now();
    },
    recordFailure(reason, previousState, audioTime=null) {
      // Cleanup and late status messages must not hide the original fault.
      if (this.failureReason || reason==='disabled' || reason==='session_replaced') return;
      this.failureReason=reason; this.failureState=previousState;
      this.firstFailureWall=this.audioWall(audioTime);
    },
    status() {
      return {state:this.mode, reason:this.reason, session_nonce:this.nonce, generation:this.generation,
        sample_rate:16000, frame_samples:cfg.frame_samples, captured_frames:this.captured,
        processed_frames:this.processed, played_frames:this.played, dropped_frames:this.dropped,
        buffered_input_frames:this.queue.length + this.offered.size,
        buffered_output_frames:this.outputCount, processed_only_verified:false,
        failure_reason:this.failureReason, failure_state:this.failureState,
        first_failure_wall_ms:this.firstFailureWall,
        first_input_wall_ms:this.firstInputWall, last_input_wall_ms:this.lastInputWall,
        first_playback_wall_ms:this.firstPlaybackWall, last_playback_wall_ms:this.lastPlaybackWall,
        last_output_wall_ms:this.lastOutputWall,
        last_input_age_ms:this.lastInputAt===null ? null : Math.max(0,performance.now()-this.lastInputAt),
        last_output_age_ms:this.lastOutputAt===null ? null : Math.max(0,performance.now()-this.lastOutputAt),
        last_input_rms:this.lastInputRms, last_output_rms:this.lastOutputRms,
        audio_context_time_seconds:this.audioTime,
        native_restore_requires_reload:true};
    },
    bypass(reason) {
      this.recordFailure(reason,this.mode);
      this.queue=[]; this.offered.clear(); this.mode='bypassed'; this.reason=reason;
      this.graph.node.port.postMessage({kind:'bypass', session_nonce:this.nonce,
        generation:this.generation, reason});
    },
    stop(reason) {
      if(this.stopped) return this.status();
      this.stopped=true; if(this.timer) clearInterval(this.timer);
      const selected = typeof reason==='string' ? reason
        : reason && typeof reason.reason==='string' ? reason.reason : 'disabled';
      this.bypass(selected); this.mode='stopped';
      return this.status();
    },
    pull(value) {
      this.lastPoll=performance.now();
      if(this.stopped) return {...this.status(),frames:[]};
      if (!this.valid()) { this.bypass('source_changed'); return {...this.status(),frames:[]}; }
      const count=Math.min(8,Math.max(1,value.max_frames)); const frames=[];
      while (frames.length<count && this.queue.length) {
        const frame=this.queue.shift(); this.offered.set(frame.sequence,frame.start_sample);
        const bytes=new Uint8Array(frame.samples.length*2); const view=new DataView(bytes.buffer);
        for(let i=0;i<frame.samples.length;i++) view.setInt16(i*2,
          Math.max(-32768,Math.min(32767,Math.round(frame.samples[i]*32768))),true);
        let binary=''; for(const byte of bytes) binary+=String.fromCharCode(byte);
        frames.push({sequence:frame.sequence,start_sample:frame.start_sample,
          sample_count:cfg.frame_samples,pcm_s16le:btoa(binary)});
      }
      return {...this.status(),frames};
    },
    push(value) {
      this.lastPoll=performance.now();
      if (!this.valid() || this.mode==='bypassed') return {...this.status(),accepted_frames:0};
      const decoded=[]; const offered=Array.from(this.offered.entries());
      for(let index=0;index<value.frames.length;index++) {
        const frame=value.frames[index], expected=offered[index];
        if(!expected || frame.sequence!==expected[0] || frame.start_sample!==expected[1] ||
          typeof frame.pcm_s16le!=='string' || frame.pcm_s16le.length!==Math.ceil(cfg.frame_samples*2/3)*4)
          return {...this.status(),accepted_frames:0,error:'invalid_output'};
        let binary; try { binary=atob(frame.pcm_s16le); } catch(_) {
          return {...this.status(),accepted_frames:0,error:'invalid_output'};
        }
        if(binary.length!==cfg.frame_samples*2) return {...this.status(),accepted_frames:0,error:'invalid_output'};
        const bytes=Uint8Array.from(binary, character=>character.charCodeAt(0));
        const view=new DataView(bytes.buffer), samples=new Float32Array(cfg.frame_samples);
        for(let i=0;i<samples.length;i++) samples[i]=view.getInt16(i*2,true)/32768;
        decoded.push({sequence:frame.sequence,samples});
      }
      if(decoded.length) {
        // Measure before transferring: the sender's PCM ArrayBuffer is detached by MessagePort.
        this.lastOutputAt=performance.now();this.lastOutputWall=Date.now();
        const samples=decoded[decoded.length-1].samples;
        let energy=0;for(const sample of samples)energy+=sample*sample;
        this.lastOutputRms=Math.sqrt(energy/samples.length);
      }
      for(const frame of decoded) {
        this.offered.delete(frame.sequence);
        this.graph.node.port.postMessage({kind:'output',session_nonce:this.nonce,generation:this.generation,
          sequence:frame.sequence,samples:frame.samples},[frame.samples.buffer]);
      }
      return {...this.status(),accepted_frames:decoded.length};
    }
  };
  graph.node.port.onmessage=event=>{
    const value=event.data;
    if (!value || !state.matches(value) || state.stopped) return;
    if (value.kind==='status') {
      if(value.failure_reason) state.recordFailure(value.failure_reason,value.failure_state,
        value.failure_audio_time_seconds);
      // A queued render-thread report must not reactivate a failed main-thread session.
      if(state.mode!=='bypassed' || value.state==='bypassed') {
        state.mode=value.state; state.reason=value.reason;
      }
      state.processed=value.processed_frames;
      state.played=value.played_frames; state.outputCount=value.buffered_output_frames;
      state.audioTime=Number.isFinite(value.audio_context_time_seconds) ? value.audio_context_time_seconds : null;
      if(Number.isFinite(value.first_playback_audio_time_seconds) && state.firstPlaybackWall===null)
        state.firstPlaybackWall=state.audioWall(value.first_playback_audio_time_seconds);
      if(Number.isFinite(value.last_playback_audio_time_seconds))
        state.lastPlaybackWall=state.audioWall(value.last_playback_audio_time_seconds);
      if(state.mode==='bypassed') {state.queue=[];state.offered.clear();}
    } else if(value.kind==='input' && state.valid() && state.mode!=='bypassed') {
      if(!(value.samples instanceof Float32Array) || value.samples.length!==cfg.frame_samples ||
        !Number.isSafeInteger(value.sequence) || value.sequence!==state.lastSequence+1 ||
        value.start_sample!==value.sequence*cfg.frame_samples) {state.bypass('invalid_capture');return;}
      if(state.queue.length+state.offered.size>=cfg.max_frames) {
        state.dropped++;state.bypass('capture_overflow');return;
      }
      state.lastSequence=value.sequence;state.captured++;state.queue.push(value);
      state.lastInputAt=performance.now();state.lastInputWall=state.audioWall(value.audio_time_seconds);
      if(state.firstInputWall===null) state.firstInputWall=state.audioWall(value.first_input_audio_time_seconds);
      state.lastInputRms=Number.isFinite(value.rms) ? value.rms : 0;
    }
  };
  manager.active=state;
  graph.node.port.postMessage({kind:'configure',...cfg});
  state.timer=setInterval(()=>{
    if(state.stopped) return;
    if(!state.valid()) state.stop('source_changed');
    else if(performance.now()-state.lastPoll>2000) state.stop('controller_timeout');
  },250);
  return state.status();
})()
"""


def install_script(session_nonce: str, generation: int, url: str, *,
                   frame_samples: int = FRAME_SAMPLES, prebuffer_frames: int = 2) -> str:
    cfg = _identity(session_nonce, generation)
    if type(frame_samples) is not int or frame_samples != FRAME_SAMPLES:
        raise ValueError("invalid_audio_frame_size")
    if type(prebuffer_frames) is not int or not 1 <= prebuffer_frames <= 5:
        raise ValueError("invalid_audio_prebuffer")
    cfg.update(url=validate_source_url(url), sample_rate=SAMPLE_RATE, frame_samples=frame_samples,
               max_frames=MAX_BUFFER_FRAMES, prebuffer_frames=prebuffer_frames)
    return _INSTALL.replace("__CFG__", _json(cfg)).replace("__WORKLET__", _json(_WORKLET))


def _command(session_nonce: str, generation: int, command: str, **fields) -> str:
    cfg = _identity(session_nonce, generation)
    cfg.update(fields)
    return """(() => {const cfg=%s, manager=window[%s], active=manager && manager.active;
      if(!active || !active.matches(cfg)) return {state:'unavailable',reason:'stale_session',frames:[],accepted_frames:0};
      return active.%s(cfg);})()""" % (_json(cfg), _json(BRIDGE_KEY), command)


def pull_script(session_nonce: str, generation: int, *, max_frames: int = 8) -> str:
    if type(max_frames) is not int or not 1 <= max_frames <= 8:
        raise ValueError("invalid_audio_pull_size")
    return _command(session_nonce, generation, "pull", max_frames=max_frames)


def push_script(session_nonce: str, generation: int, frames: list[dict]) -> str:
    if not isinstance(frames, list) or not 1 <= len(frames) <= 8:
        raise ValueError("invalid_audio_frames")
    safe = []
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("invalid_audio_frame")
        sequence, start, payload = frame.get("sequence"), frame.get("start_sample"), frame.get("pcm_s16le")
        if (type(sequence) is not int or not 0 <= sequence <= 2147483647 or type(start) is not int
                or start != sequence * FRAME_SAMPLES or not isinstance(payload, str)
                or len(payload) != ((FRAME_SAMPLES * 2 + 2) // 3) * 4):
            raise ValueError("invalid_audio_frame")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            raise ValueError("invalid_audio_frame") from None
        if len(raw) != FRAME_SAMPLES * 2:
            raise ValueError("invalid_audio_frame")
        if safe and sequence != safe[-1]["sequence"] + 1:
            raise ValueError("invalid_audio_frame_order")
        safe.append({"sequence": sequence, "start_sample": start, "pcm_s16le": payload})
    return _command(session_nonce, generation, "push", frames=safe)


def status_script(session_nonce: str, generation: int) -> str:
    return _command(session_nonce, generation, "status")


def stop_script(session_nonce: str, generation: int, *, reason: str = "disabled") -> str:
    if not isinstance(reason, str) or reason not in STOP_REASONS:
        raise ValueError("invalid_audio_stop_reason")
    return _command(session_nonce, generation, "stop", reason=reason)
