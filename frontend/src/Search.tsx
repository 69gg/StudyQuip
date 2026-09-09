import SearchPanel from "./SearchPanel";
import type { Book, Subject } from "./types";
export default function Search({
  subjects,
  books,
}: {
  subjects: Subject[];
  books: Book[];
}) {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>检索</h1>
          <p>查找教材原文，或按题目部分寻找相关错题。</p>
        </div>
      </div>
      <SearchPanel subjects={subjects} books={books} />
    </>
  );
}
