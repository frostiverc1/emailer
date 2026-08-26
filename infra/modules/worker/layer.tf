# Jinja2 Lambda layer, built locally with pip since Terraform runs locally for the pilot.
# Requires `pip` to be available on the machine running `terraform apply`.

resource "null_resource" "jinja2_layer_build" {
  triggers = {
    requirements_hash = filesha256("${path.module}/../../../src/worker/requirements.txt")
  }

  provisioner "local-exec" {
    command = "pip install -r ${path.module}/../../../src/worker/requirements.txt -t ${path.module}/../../../build/layer/python --upgrade"
  }
}

data "archive_file" "jinja2_layer" {
  type        = "zip"
  source_dir  = "${path.module}/../../../build/layer"
  output_path = "${path.module}/../../../build/jinja2-layer.zip"

  depends_on = [null_resource.jinja2_layer_build]
}

resource "aws_lambda_layer_version" "jinja2" {
  layer_name          = "${var.name_prefix}-jinja2"
  filename            = data.archive_file.jinja2_layer.output_path
  source_code_hash    = data.archive_file.jinja2_layer.output_base64sha256
  compatible_runtimes = ["python3.12"]
}
