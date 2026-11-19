import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import { BookOutlined, EyeOutlined, UploadOutlined } from "@ant-design/icons";
import type { UploadFile } from "antd";
import { api } from "../../shared/api/client";
import type { Novel } from "../../shared/types/api";

interface Props {
  selectedNovelId: string | null;
  onSelectNovel: (novel: Novel) => void;
}

export function NovelsPage({ selectedNovelId, onSelectNovel }: Props) {
  const qc = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [form] = Form.useForm();
  const [chapterPage, setChapterPage] = useState(1);
  const [chapterSearch, setChapterSearch] = useState("");
  const [readingChapterId, setReadingChapterId] = useState<string | null>(null);

  const novelsQuery = useQuery({
    queryKey: ["novels"],
    queryFn: () => api.listNovels(1, 50),
  });

  const chaptersQuery = useQuery({
    queryKey: ["chapters", selectedNovelId, chapterPage, chapterSearch],
    queryFn: () => api.listChapters(selectedNovelId!, chapterPage, 30, chapterSearch || undefined),
    enabled: !!selectedNovelId,
  });

  const chapterDetailQuery = useQuery({
    queryKey: ["chapter-detail", readingChapterId],
    queryFn: () => api.getChapter(readingChapterId!),
    enabled: !!readingChapterId,
  });

  const uploadMut = useMutation({
    mutationFn: async (values: { title: string; author?: string }) => {
      const f = fileList[0]?.originFileObj;
      if (!f) throw new Error("请选择 TXT 或 EPUB 文件");
      return api.uploadNovel(f, values.title, values.author);
    },
    onSuccess: (novel) => {
      message.success(`导入成功：共解析 ${novel.chapter_count} 章，${novel.chunk_count} 个文本块`);
      setUploadOpen(false);
      setFileList([]);
      form.resetFields();
      qc.invalidateQueries({ queryKey: ["novels"] });
      onSelectNovel(novel);
    },
    onError: (err: Error) => message.error(err.message),
  });

  const activeNovel = novelsQuery.data?.items.find((n) => n.id === selectedNovelId);

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card
        title={
          <Space>
            <BookOutlined />
            <span>小说项目管理</span>
          </Space>
        }
        extra={
          <Button type="primary" icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
            导入小说 (TXT / EPUB)
          </Button>
        }
      >
        <Table
          rowKey="id"
          loading={novelsQuery.isLoading}
          dataSource={novelsQuery.data?.items || []}
          pagination={false}
          columns={[
            {
              title: "书名",
              dataIndex: "title",
              render: (text: string, record: Novel) => (
                <Space>
                  <Typography.Link strong onClick={() => onSelectNovel(record)}>
                    {text}
                  </Typography.Link>
                  {record.id === selectedNovelId && <Tag color="blue">当前选中</Tag>}
                </Space>
              ),
            },
            { title: "作者", dataIndex: "author", render: (v) => v || "-" },
            { title: "编码", dataIndex: "encoding" },
            { title: "章节数", dataIndex: "chapter_count" },
            { title: "分块数", dataIndex: "chunk_count" },
            {
              title: "总字数",
              dataIndex: "total_chars",
              render: (v: number) => v.toLocaleString(),
            },
            {
              title: "数据版本",
              dataIndex: "data_revision",
              render: (v: number) => <Tag>rev.{v}</Tag>,
            },
            {
              title: "操作",
              render: (_: unknown, record: Novel) => (
                <Button
                  size="small"
                  type={record.id === selectedNovelId ? "primary" : "default"}
                  onClick={() => onSelectNovel(record)}
                >
                  切换此书
                </Button>
              ),
            },
          ]}
        />
      </Card>

      {activeNovel && (
        <Card
          title={`章节目录 — ${activeNovel.title}`}
          extra={
            <Input.Search
              placeholder="搜索章节标题"
              allowClear
              style={{ width: 240 }}
              onSearch={(val) => {
                setChapterSearch(val);
                setChapterPage(1);
              }}
            />
          }
        >
          {activeNovel.diagnostics_json?.missing_chapter_numbers &&
            activeNovel.diagnostics_json.missing_chapter_numbers.length > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 16 }}
                message={`检测到可能缺失章节编号：${activeNovel.diagnostics_json.missing_chapter_numbers
                  .slice(0, 15)
                  .join(", ")}`}
              />
            )}

          <Table
            rowKey="id"
            size="small"
            loading={chaptersQuery.isLoading}
            dataSource={chaptersQuery.data?.items || []}
            pagination={{
              current: chapterPage,
              pageSize: 30,
              total: chaptersQuery.data?.total || 0,
              onChange: (p) => setChapterPage(p),
            }}
            columns={[
              { title: "序号", dataIndex: "chapter_no", width: 90 },
              { title: "章节标题", dataIndex: "title" },
              { title: "字符数", dataIndex: "char_count", width: 110 },
              {
                title: "操作",
                width: 110,
                render: (_, rec) => (
                  <Button
                    size="small"
                    icon={<EyeOutlined />}
                    onClick={() => setReadingChapterId(rec.id)}
                  >
                    预览原文
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      )}

      <Modal
        title="导入小说文件"
        open={uploadOpen}
        onCancel={() => setUploadOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={uploadMut.isPending}
      >
        <Form form={form} layout="vertical" onFinish={(v) => uploadMut.mutate(v)}>
          <Form.Item name="title" label="书名" rules={[{ required: true, message: "请输入书名" }]}>
            <Input placeholder="例如：凡人修仙之仙界篇" />
          </Form.Item>
          <Form.Item name="author" label="作者">
            <Input placeholder="例如：忘语" />
          </Form.Item>
          <Form.Item label="小说文件 (仅支持 TXT / EPUB，暂不支持 PDF)" required>
            <Upload
              beforeUpload={() => false}
              maxCount={1}
              accept=".txt,.epub"
              fileList={fileList}
              onChange={({ fileList: fl }) => setFileList(fl)}
            >
              <Button icon={<UploadOutlined />}>选择文件</Button>
            </Upload>
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={chapterDetailQuery.data?.title || "章节正文"}
        width={720}
        open={!!readingChapterId}
        onClose={() => setReadingChapterId(null)}
      >
        {chapterDetailQuery.data && (
          <>
            <Descriptions size="small" bordered column={3} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="章节序号">
                {chapterDetailQuery.data.chapter_no}
              </Descriptions.Item>
              <Descriptions.Item label="字数">
                {chapterDetailQuery.data.char_count}
              </Descriptions.Item>
              <Descriptions.Item label="分块数">
                {chapterDetailQuery.data.chunks.length}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Paragraph style={{ whiteSpace: "pre-wrap", lineHeight: 1.9 }}>
              {chapterDetailQuery.data.content}
            </Typography.Paragraph>
          </>
        )}
      </Drawer>
    </Space>
  );
}
