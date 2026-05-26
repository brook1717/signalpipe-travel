output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "api_url" {
  description = "Public URL of the FastAPI service"
  value       = "http://${aws_lb.api.dns_name}"
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint"
  value       = aws_db_instance.postgres.endpoint
}

output "redis_endpoint" {
  description = "ElastiCache Redis endpoint"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "sqs_main_queue_url" {
  description = "Main SQS FIFO queue URL (scraping tasks)"
  value       = aws_sqs_queue.scraper_main.url
}

output "sqs_alert_queue_url" {
  description = "SQS alert queue URL (price-drop events)"
  value       = aws_sqs_queue.scraper_alert.url
}

output "sqs_dlq_url" {
  description = "SQS Dead-Letter Queue URL (failed URLs after 3 attempts)"
  value       = aws_sqs_queue.scraper_dlq.url
}
