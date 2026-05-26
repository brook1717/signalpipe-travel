variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "scraper"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "scraper"
  sensitive   = true
}

variable "db_password" {
  description = "PostgreSQL master password"
  type        = string
  sensitive   = true
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "scraper"
}

variable "api_image" {
  description = "Docker image URI for the FastAPI container"
  type        = string
}

variable "worker_image" {
  description = "Docker image URI for the Celery worker container"
  type        = string
}

variable "gemini_api_key" {
  description = "Gemini API key for LLM extraction"
  type        = string
  default     = ""
  sensitive   = true
}

variable "api_cpu" {
  description = "Fargate CPU units for API task"
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory (MiB) for API task"
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = "Fargate CPU units for worker task"
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for worker task"
  type        = number
  default     = 1024
}

variable "worker_desired_count" {
  description = "Number of Celery worker tasks"
  type        = number
  default     = 2
}
