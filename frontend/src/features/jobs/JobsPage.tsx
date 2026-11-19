import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Col,
  Form,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  message,
} from "antd";
import { PauseCircleOutlined, PlayCircleOutlined, RocketOutlined } from "@ant-design/icons";
import { api, subscribeJobEvents } from "../../shared/api/client";
import type { Job } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

const STATE_COLOR: Record<string, string> = {
  pending: "default",
  running: "processing",
  paused: "warning",
  completed: "success",
  failed: "error",
  cancelled: "default",
};

export function JobsPage({ novelId }: Props) {
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const [activeStreamJobId, setActiveStreamJobId] = useState<string | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["jobs", novelId],
    queryFn: () => api.listJobs(novelId!),
    enabled: !!novelId,
    refetchInterval: 5000,
  });

  const usageQuery = useQuery({
    queryKey: ["usage", novelId],
    queryFn: () => api.getUsage(novelId!),
    enabled: !!novelId,
  });

  useEffect(() => {
    if (!activeStreamJobId) return;
    const unsub = subscribeJobEvents(activeStreamJobId, () => {
      qc.invalidateQueries({ queryKey: ["jobs", novelId] });
      qc.invalidateQueries({ queryKey: ["usage", novelId] });
    });
    return unsub;
  }, [activeStreamJobId, novelId, qc]);

  const createMut = useMutation({
    mutationFn: (values: {
      job_type: string;
      budget_limit: number;
      chapter_from?: number;
      chapter_to?: number;
    }) => api.createJob(novelId!, values),
    onSuccess: (job) => {
      message.success(`分析任务已创建 (${job.job_type})`);
      setActiveStreamJobId(job.id);
      qc.invalidateQueries({ queryKey: ["jobs", novelId] });
    },
    onError: (err: Error) => message.error(err.message),
  });

  const pauseMut = useMutation({
    mutationFn: (id: string) => api.pauseJob(id),
    onSuccess: () => {
      message.info("已请求暂停任务");
      qc.invalidateQueries({ queryKey: ["jobs", novelId] });
    },
  });

  const resumeMut = useMutation({
    mutationFn: (id: string) => api.resumeJob(id),
    onSuccess: (job) => {
      message.success("任务已恢复执行");
      setActiveStreamJobId(job.id);
      qc.invalidateQueries({ queryKey: ["jobs", novelId] });
    },
  });

  if (!novelId) {
    return <Card>请先在「小说项目」页面导入并选择一部小说。</Card>;
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Row gutter={16}>
        <Col span={8}>
          <Card>
            <Statistic
              title="累计输入 Tokens"
              value={usageQuery.data?.total_input_tokens || 0}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic
              title="累计输出 Tokens"
              value={usageQuery.data?.total_output_tokens || 0}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic
              title="预估累计费用 (USD)"
              value={usageQuery.data?.total_estimated_cost || 0}
              precision={4}
              prefix="$"
            />
          </Card>
        </Col>
      </Row>

      <Card title="发起 AI 分析任务">
        <Form
          form={form}
          layout="inline"
          initialValues={{ job_type: "FULL_PIPELINE", budget_limit: 20 }}
          onFinish={(v) => createMut.mutate(v)}
        >
          <Form.Item name="job_type" label="分析阶段">
            <Select
              style={{ width: 220 }}
              options={[
                { value: "FULL_PIPELINE", label: "全流程 (S1→S6)" },
                { value: "S1_DISCOVER", label: "S1 人物发现" },
                { value: "S2_EXTRACT", label: "S2 事实/事件/关系抽取" },
                { value: "S3_DISAMBIGUATE", label: "S3 身份消歧" },
                { value: "S4_CONSOLIDATE", label: "S4 状态冲突校验" },
                { value: "S5_ANALYZE", label: "S5 深度人物报告" },
                { value: "S6_PUBLISH", label: "S6 核验发布" },
              ]}
            />
          </Form.Item>
          <Form.Item name="chapter_from" label="起始章">
            <InputNumber min={1} placeholder="全部" style={{ width: 90 }} />
          </Form.Item>
          <Form.Item name="chapter_to" label="结束章">
            <InputNumber min={1} placeholder="全部" style={{ width: 90 }} />
          </Form.Item>
          <Form.Item name="budget_limit" label="预算上限 ($)">
            <InputNumber min={0} step={1} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              icon={<RocketOutlined />}
              loading={createMut.isPending}
            >
              启动任务
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Card title="任务列表">
        <Table
          rowKey="id"
          loading={jobsQuery.isLoading}
          dataSource={jobsQuery.data?.items || []}
          columns={[
            { title: "任务阶段", dataIndex: "job_type" },
            {
              title: "状态",
              dataIndex: "state",
              render: (s: string) => <Tag color={STATE_COLOR[s] || "default"}>{s}</Tag>,
            },
            {
              title: "进度",
              render: (_: unknown, rec: Job) => {
                const total = rec.progress_json?.total_tasks || 0;
                const done = rec.progress_json?.completed_tasks || 0;
                const pct = total > 0 ? Math.round((done / total) * 100) : rec.state === "completed" ? 100 : 0;
                return (
                  <Space direction="vertical" size={0} style={{ width: 180 }}>
                    <Progress percent={pct} size="small" />
                    <span style={{ fontSize: 12, color: "#888" }}>
                      {done}/{total} 完成 · {rec.progress_json?.failed_tasks || 0} 失败
                    </span>
                  </Space>
                );
              },
            },
            {
              title: "已耗 / 预算 ($)",
              render: (_: unknown, rec: Job) =>
                `$${rec.spent_cost.toFixed(4)} / $${rec.budget_limit.toFixed(2)}`,
            },
            {
              title: "操作",
              render: (_: unknown, rec: Job) => (
                <Space>
                  {rec.state === "running" && (
                    <Button
                      size="small"
                      icon={<PauseCircleOutlined />}
                      onClick={() => pauseMut.mutate(rec.id)}
                    >
                      暂停
                    </Button>
                  )}
                  {(rec.state === "paused" || rec.state === "failed") && (
                    <Button
                      size="small"
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={() => resumeMut.mutate(rec.id)}
                    >
                      恢复
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
