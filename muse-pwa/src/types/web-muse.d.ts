// src/types/web-muse.d.ts

declare module 'web-muse' {
  export class MuseCircularBuffer {
    memory: number[];
    head: number;
    tail: number;
    isFull: boolean;
    lastwrite: number;
    length: number;
    constructor(size: number);
    read(): number | null;
    write(value: number): void;
    next(n: number): number;
  }

  export class MuseBase {
    state: number;
    mock: boolean;
    constructor(options?: { mock?: boolean; mockDataPath?: string });
    connect(): Promise<void>;
    disconnect(): void;
  }

  export class Muse extends MuseBase {
    batteryLevel: number | null;
    info: Record<string, any>;
    eeg: MuseCircularBuffer[];
    ppg: MuseCircularBuffer[];
    accelerometer: MuseCircularBuffer[];
    gyroscope: MuseCircularBuffer[];
    constructor(options?: { mock?: boolean; mockDataPath?: string });
  }

  export function connectMuse(options?: { mock?: boolean; mockDataPath?: string }): Promise<Muse>;
}
