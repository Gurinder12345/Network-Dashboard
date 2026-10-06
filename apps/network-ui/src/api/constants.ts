export const OS6_PLATFORM = "dell_os6";
export const OS10_PLATFORM = "dell_os10";

// Platforms the guarded change workflow supports. The worker enforces each platform's
// command policy; Dell OS10 V1 allows only interface descriptions.
export const CHANGE_PLATFORMS = [OS6_PLATFORM, OS10_PLATFORM];

// Mirrors the worker's HEALTH_SLOW_THRESHOLD_MS default: a successful check slower than
// this is classified "degraded". The UI only visualizes it; the worker decides status.
export const HEALTH_SLOW_THRESHOLD_MS = 15000;
