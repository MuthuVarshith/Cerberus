output "public_ip" {
  description = "Elastic IP of the Cerberus instance."
  value       = aws_eip.cerberus.public_ip
}

output "webhook_url" {
  description = "Paste into the GitHub App settings as the Webhook URL."
  value       = var.domain_name != "" ? "https://${var.domain_name}/webhook" : "http://${aws_eip.cerberus.public_ip}:8000/webhook"
}

output "instance_id" {
  description = "For Session Manager: aws ssm start-session --target <instance_id>"
  value       = aws_instance.cerberus.id
}
