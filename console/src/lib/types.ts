// Service health status
export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'unknown';

// Media Bundle Types
export interface MediaStatus {
  timestamp: string;
  library: {
    series: { total: number; episodes: number; missing: number };
    movies: { total: number; missing: number };
    books: { total: number; missing: number };
  };
  queue: {
    downloading: number;
    seeding: number;
    stalled: number;
    total: number;
  };
  storage: {
    freeSpace: string;
    freeSpaceBytes: number;
  };
  activity: {
    downloadSpeed: string;
    uploadSpeed: string;
  };
}

export interface ServiceHealth {
  status: HealthStatus;
  version?: string;
  issues?: string[];
}

export interface MediaHealth {
  status: HealthStatus;
  timestamp: string;
  services: {
    jellyfin: ServiceHealth;
    sonarr: ServiceHealth;
    radarr: ServiceHealth;
    prowlarr: ServiceHealth;
    readarr: ServiceHealth;
    qbittorrent: ServiceHealth;
  };
}

// Smart Home Types
export interface SmartHomeStatus {
  connected: boolean;
  summary: {
    total_entities: number;
    by_domain: Record<string, number>;
    online_devices: number;
    offline_devices: number;
  };
}

// Dashboard Card
export interface ServiceCard {
  id: string;
  name: string;
  icon: string;
  status: HealthStatus;
  url?: string;
  stats?: { label: string; value: string | number }[];
}

// Bundle configuration
export interface BundleConfig {
  media: {
    host: string;
    services: {
      jellyfin: { port: number };
      sonarr: { port: number; apiKey: string };
      radarr: { port: number; apiKey: string };
      prowlarr: { port: number; apiKey: string };
      readarr: { port: number; apiKey: string };
      qbittorrent: { port: number };
    };
  };
  smarthome: {
    host: string;
    port: number;
    token?: string;
  };
}
