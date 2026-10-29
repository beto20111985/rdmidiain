/**
 * Watchdog web (A4 §9.1; P001 §73): detecta
 *  - quadro congelado: requestAnimationFrame sem disparar por > 5 s com a página visível;
 *  - vídeo parado: currentTime sem avançar por > 6 s durante a reprodução, ou `waiting` por > 8 s;
 * e reage: 1º) recria as camadas e reentra na grade (engine.recover); 2º) se houver 3 recuperações em 10 min,
 * `location.reload()`. Watchdog de PROCESSO depende da casca (Windows/Android) — o bundle envia `progress`.
 */

interface VideoProbe {
  frontVideo(): HTMLVideoElement | null;
}

export interface WatchdogOptions {
  renderer: VideoProbe;
  onRecover: (reason: string) => void;
  onReload: (reason: string) => void;
  onFrozen?: (frozen: boolean) => void;
  isVisible?: () => boolean;
  rafStallMs?: number;
  videoStallMs?: number;
  waitingMs?: number;
  maxRecoveries?: number;
  windowMs?: number;
}

export class WebWatchdog {
  private lastFrame = performance.now();
  private rafId = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private video: HTMLVideoElement | null = null;
  private lastTime = -1;
  private lastAdvance = performance.now();
  private waitingSince: number | null = null;
  private cooldownUntil = 0;
  private recoveries: number[] = [];
  private frozen = false;
  private readonly o: Required<Omit<WatchdogOptions, 'onFrozen'>> & Pick<WatchdogOptions, 'onFrozen'>;

  constructor(opts: WatchdogOptions) {
    this.o = {
      isVisible: () => document.visibilityState === 'visible',
      rafStallMs: 5_000,
      videoStallMs: 6_000,
      waitingMs: 8_000,
      maxRecoveries: 3,
      windowMs: 10 * 60_000,
      ...opts,
    };
  }

  start(): void {
    const loop = (t: number): void => {
      this.lastFrame = t;
      this.rafId = requestAnimationFrame(loop);
    };
    this.rafId = requestAnimationFrame(loop);
    this.timer = setInterval(() => this.check(), 1_000);
  }

  stop(): void {
    cancelAnimationFrame(this.rafId);
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private onWaiting = (): void => {
    this.waitingSince ??= performance.now();
  };
  private onPlaying = (): void => {
    this.waitingSince = null;
  };

  private track(v: HTMLVideoElement | null): void {
    if (v === this.video) return;
    if (this.video) {
      this.video.removeEventListener('waiting', this.onWaiting);
      this.video.removeEventListener('playing', this.onPlaying);
    }
    this.video = v;
    this.lastTime = -1;
    this.lastAdvance = performance.now();
    this.waitingSince = null;
    if (v) {
      v.addEventListener('waiting', this.onWaiting);
      v.addEventListener('playing', this.onPlaying);
    }
  }

  private check(): void {
    const now = performance.now();
    if (!this.o.isVisible()) {
      this.lastFrame = now;
      this.lastAdvance = now;
      return;
    }
    if (now < this.cooldownUntil) return;
    if (now - this.lastFrame > this.o.rafStallMs) {
      this.trigger('RAF_FROZEN');
      return;
    }
    const v = this.o.renderer.frontVideo();
    this.track(v);
    if (v && !v.paused && !v.ended) {
      if (v.currentTime !== this.lastTime) {
        this.lastTime = v.currentTime;
        this.lastAdvance = now;
      } else if (now - this.lastAdvance > this.o.videoStallMs) {
        this.trigger('VIDEO_STALL');
        return;
      }
      if (this.waitingSince !== null && now - this.waitingSince > this.o.waitingMs) {
        this.trigger('VIDEO_WAITING');
        return;
      }
    }
    if (this.frozen) {
      this.frozen = false;
      this.o.onFrozen?.(false);
    }
  }

  private trigger(reason: string): void {
    const now = performance.now();
    this.cooldownUntil = now + 5_000;
    this.recoveries = this.recoveries.filter((t) => now - t < this.o.windowMs);
    this.recoveries.push(now);
    this.frozen = true;
    this.o.onFrozen?.(true);
    this.track(null);
    if (this.recoveries.length >= this.o.maxRecoveries) {
      this.o.onReload(reason);
    } else {
      this.o.onRecover(reason);
    }
  }
}
