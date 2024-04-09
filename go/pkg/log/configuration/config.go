package configuration

import "os"

type LogServiceConfiguration struct {
	PORT         string
	DATABASE_URL string
}

func getEnvWithDefault(key, defaultValue string) string {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	return value
}

func NewLogServiceConfiguration() *LogServiceConfiguration {
	return &LogServiceConfiguration{
		PORT:         getEnvWithDefault("PORT", "50051"),
		DATABASE_URL: getEnvWithDefault("CHROMA_DATABASE_URL", "postgresql://chroma:chroma@postgres.chroma.svc.cluster.local:5432/log"),
	}
}
