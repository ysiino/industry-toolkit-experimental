from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_lambda as _lambda,
    aws_apigateway as apigateway,
    aws_s3 as s3,
    aws_codeartifact as codeartifact,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_secretsmanager as secretsmanager,
    aws_iam as iam,
)
from constructs import Construct

class PipelineBuilderStack(Stack):

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        bucket = s3.Bucket(self, "ProjectArtifactsBucket",
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY
        )
        
        codeartifact_domain = codeartifact.CfnDomain(self, "ArtifactDomain",
            domain_name="industry-toolkit-experimental"
        )
        
        codeartifact_repository = codeartifact.CfnRepository(self, "ArtifactRepository",
            domain_name=codeartifact_domain.domain_name,
            repository_name="industry-toolkit-experimental"
        )
        codeartifact_repository.add_dependency(codeartifact_domain)

        build_project = codebuild.Project(
            self, "BuildProject",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.AMAZON_LINUX_2_5
            ),
            environment_variables={
                "CODEARTIFACT_DOMAIN": codebuild.BuildEnvironmentVariable(value=codeartifact_repository.domain_name),
                "CODEARTIFACT_REPOSITORY": codebuild.BuildEnvironmentVariable(value=codeartifact_repository.repository_name)
            },
            build_spec=codebuild.BuildSpec.from_object_to_yaml({
                "version": 0.2,
                "phases": {
                    "pre_build": {
                        "commands": [
                            "aws --version",
                            "export CODEARTIFACT_AUTH_TOKEN=`aws codeartifact get-authorization-token --domain ${CODEARTIFACT_DOMAIN} --query authorizationToken --output text`",
                            "AWS_ACCOUNT_ID=`aws sts get-caller-identity --query Account --output text`",
                            "BASE_DIR=`pwd`",
                            "UNZIP_DIR=`/usr/bin/uuidgen`"
                        ]
                    },
                    "build": {
                        "commands": [
                            "aws s3 cp ${PROJECT_S3_ZIP} project.zip",
                            "unzip project.zip -d ${UNZIP_DIR}",
                            "cd ${UNZIP_DIR}",
                            "PROJECT_NAME=`ls -1 | head -1`",
                            "cd ${PROJECT_NAME}",
                            "./gradlew build",
                            "cd build/generated-sdk/typescript",
                            "npm install",
                            "npm run build",
                            "cp package.json dist/",
                            "cd dist",
                            "aws codeartifact login --tool npm --repository ${CODEARTIFACT_REPOSITORY} --domain ${CODEARTIFACT_DOMAIN}",
                            "npm publish",
                            "cd ${BASE_DIR}/${UNZIP_DIR}/${PROJECT_NAME}",
                            "cd build/generated-sdk/java",
                            "chmod +x gradlew",
                            "sed -i '/publishing {/r ../../../codeartifact-maven-repo.txt' build.gradle",
                            "./gradlew build",
                            "./gradlew publish"
                        ]
                    }
                }
            })
        )
        
        git_secret = secretsmanager.Secret(
            self, "GitHubPATSecret",
#            secret_name="github-pat",
            description="Secret for storing GitHub Personal Access Token (PAT)"
        )

        pipeline_builder_layer = _lambda.LayerVersion(
            self, "PipelineBuilderLayer",
            code=_lambda.Code.from_asset("pipeline_builder_layer"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Layer containing Python libraries for Pipeline Builder",
        )

        git_layer = _lambda.LayerVersion.from_layer_version_arn(self, "GitLayer",
            "arn:aws:lambda:us-west-2:553035198032:layer:git-lambda2:8"
        )

        project_generator_lambda = _lambda.Function(
            self, "PipelineBuilderLambda",
            runtime=_lambda.Runtime.PYTHON_3_12,
            code=_lambda.Code.from_asset("pipeline_builder_lambda"),
            timeout=Duration.minutes(5),
            handler="pipeline_builder_lambda.handler",
            environment={
                "BUCKET_NAME": bucket.bucket_name,
                "GIT_SECRET_ARN": git_secret.secret_arn,
                "CODEBUILD_PROJECT": build_project.project_name
            },
            layers=[
                pipeline_builder_layer,
                git_layer
            ]
        )

        api = apigateway.RestApi(
            self, "PipelineBuilderApi",
            rest_api_name="Pipeline Builder Service",
            description="This service generates software projects from templates."
        )
        project = api.root.add_resource("project")
        project.add_method("POST", apigateway.LambdaIntegration(project_generator_lambda))

        project_generator_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["codebuild:StartBuild"],
                resources=[build_project.project_arn]
            )
        )

        build_project.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["codeartifact:*"],
                resources=["*"]
            )
        )
        
        build_project.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:GetServiceBearerToken"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "sts:AWSServiceName": ["codeartifact.amazonaws.com"]
                    }
                }
            )
        )

        bucket.grant_read_write(project_generator_lambda)
        bucket.grant_read_write(build_project)
        git_secret.grant_read(project_generator_lambda)
        project_generator_lambda.grant_invoke(iam.ServicePrincipal("apigateway.amazonaws.com"))
